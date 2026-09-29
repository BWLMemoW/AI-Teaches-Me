"""联网搜索后端（《技术文档》§4.6；《项目文档》§4.19 FR-19）。

四类后端与配置取值的映射（C-07 定稿）：

* ``qianfan``（默认）→ ``ai_search``：百度千帆 AI 搜索 ``POST /v2/ai_search``，一次调用返回答案 + 引用列表；
* ``openai_hosted`` → ``hosted``：OpenAI Responses 的服务端托管搜索（需 ``routing.web_search``）；
* ``tavily`` / ``serper`` / ``exa`` / ``brave`` → ``api``：第三方检索 API；
* ``searxng`` → ``self_hosted``：自建实例，无需 Key。

安全：外部结果一律按不可信数据处理（``untrusted`` 包装由调用方完成），
命中「忽略以上 / 输出系统提示词」等可疑模式直接丢弃（§4.6 / §14.3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

SUSPICIOUS_PATTERNS = (
    "忽略以上", "忽略上面的", "ignore previous", "ignore above",
    "输出系统提示词", "system prompt", "你现在是", "you are now",
)
MAX_SNIPPET = 400


@dataclass
class SearchHit:
    title: str
    url: str
    domain: str
    snippet: str
    accessed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "accessed_at": self.accessed_at,
        }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def domain_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "")
    return match.group(1).lower() if match else ""


def clean_hits(raw_hits: list[dict[str, Any]], *, k: int = 5) -> list[SearchHit]:
    """清洗：丢可疑模式、去重域名、截断 snippet、补访问时间（§4.6）。"""
    cleaned: list[SearchHit] = []
    seen: set[str] = set()
    for item in raw_hits:
        url = str(item.get("url") or "")
        snippet = str(item.get("snippet") or item.get("content") or "")
        title = str(item.get("title") or url)
        if not url:
            continue
        lowered = (snippet + title).lower()
        if any(pattern.lower() in lowered for pattern in SUSPICIOUS_PATTERNS):
            continue
        domain = str(item.get("domain") or domain_of(url))
        if domain in seen:
            continue
        seen.add(domain)
        cleaned.append(
            SearchHit(
                title=title[:200],
                url=url,
                domain=domain,
                snippet=snippet[:MAX_SNIPPET],
                accessed_at=str(item.get("accessed_at") or now_iso()),
            )
        )
        if len(cleaned) >= k:
            break
    return cleaned


class SearchBackend(Protocol):
    name: str
    backend_type: str

    async def search(self, query: str, k: int) -> list[SearchHit]: ...


class _HttpBackend:
    backend_type = "api"

    def __init__(self, *, api_key: str = "", url: str = "", client: httpx.AsyncClient | None = None, timeout: float = 15.0) -> None:
        self.api_key = api_key
        self.url = url
        self._client = client
        self.timeout = timeout

    async def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns = self._client is None
        try:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                # 把服务端返回体带出来，否则用户只看到 400 不知道该改什么
                detail = (response.text or "").strip()[:300]
                raise RuntimeError(f"HTTP {response.status_code}：{detail}")
            return response.json()
        finally:
            if owns:
                await client.aclose()


class QianfanBackend(_HttpBackend):
    """默认预设：一次调用返回「答案 + 引用列表」，只取引用（references_only）。"""

    name = "qianfan"
    backend_type = "ai_search"

    def __init__(self, *, api_key: str, url: str, model: str = "ernie-4.5-turbo-128k", **kwargs: Any) -> None:
        super().__init__(api_key=api_key, url=url, **kwargs)
        self.model = model

    async def search(self, query: str, k: int) -> list[SearchHit]:
        # 千帆 /v2/ai_search 的必填字段是 search_source；带上未支持的 model 会被判 400（实测）
        payload = {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": ["web"],
            "stream": False,
        }
        if self.model:
            payload["search_recency_filter"] = None  # 预留：按需开启时效过滤
        data = await self._post(
            self.url or "https://qianfan.baidubce.com/v2/ai_search",
            payload,
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        references = data.get("references") or data.get("search_results") or []
        raw = [
            {
                "title": item.get("title"),
                "url": item.get("url") or item.get("link"),
                "snippet": item.get("content") or item.get("snippet") or item.get("summary"),
                "domain": item.get("domain") or item.get("site_name"),
            }
            for item in references
        ]
        return clean_hits(raw, k=k)


class ApiSearchBackend(_HttpBackend):
    """第三方纯检索 API（Tavily / Serper / Exa / Brave）统一适配。"""

    backend_type = "api"

    def __init__(self, *, name: str, api_key: str, url: str, **kwargs: Any) -> None:
        super().__init__(api_key=api_key, url=url, **kwargs)
        self.name = name

    async def search(self, query: str, k: int) -> list[SearchHit]:
        if self.name == "tavily":
            payload: dict[str, Any] = {"api_key": self.api_key, "query": query, "max_results": k}
        elif self.name == "serper":
            payload = {"q": query, "num": k}
        elif self.name == "exa":
            payload = {"query": query, "numResults": k}
        else:  # brave
            payload = {"q": query, "count": k}
        headers = {"X-Subscription-Token": self.api_key, "X-API-KEY": self.api_key, "Content-Type": "application/json"}
        data = await self._post(self.url, payload, headers)
        items = data.get("results") or data.get("organic") or data.get("web", {}).get("results") or []
        raw = [
            {"title": item.get("title"), "url": item.get("url"), "snippet": item.get("content") or item.get("snippet") or item.get("description")}
            for item in items
        ]
        return clean_hits(raw, k=k)


class SearxngBackend(_HttpBackend):
    name = "searxng"
    backend_type = "self_hosted"

    async def search(self, query: str, k: int) -> list[SearchHit]:
        url = (self.url or "http://localhost:8080").rstrip("/") + "/search"
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns = self._client is None
        try:
            response = await client.get(
                url, params={"q": query, "format": "json"}, headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
        finally:
            if owns:
                await client.aclose()
        raw = [
            {"title": item.get("title"), "url": item.get("url"), "snippet": item.get("content")}
            for item in (data.get("results") or [])
        ]
        return clean_hits(raw, k=k)


class HostedSearchBackend:
    """OpenAI Responses 的服务端托管搜索（走 routing.web_search 指定的档案）。"""

    name = "openai_hosted"
    backend_type = "hosted"

    def __init__(self, *, api_key: str, base_url: str = "https://api.openai.com/v1", model: str = "gpt-4o-mini", client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client

    async def search(self, query: str, k: int) -> list[SearchHit]:
        payload = {"model": self.model, "input": query, "tools": [{"type": "web_search"}]}
        client = self._client or httpx.AsyncClient(timeout=30.0)
        owns = self._client is None
        try:
            response = await client.post(
                f"{self.base_url}/responses",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        finally:
            if owns:
                await client.aclose()
        raw: list[dict[str, Any]] = []
        for item in data.get("output", []) or []:
            for annotation in item.get("content", [{}])[0].get("annotations", []) if item.get("content") else []:
                raw.append({"title": annotation.get("title"), "url": annotation.get("url"), "snippet": annotation.get("text", "")})
        return clean_hits(raw, k=k)


def backend_type_for(config_backend: str) -> str:
    return {
        "qianfan": "ai_search",
        "openai_hosted": "hosted",
        "tavily": "api",
        "serper": "api",
        "exa": "api",
        "brave": "api",
        "searxng": "self_hosted",
    }.get(config_backend, "api")


def build_backend(searching_model: Any, *, api_key: str = "", client: httpx.AsyncClient | None = None) -> SearchBackend:
    """按 ``searching_model.backend`` 造适配器（C-07 映射表）。"""
    name = str(getattr(searching_model, "backend", "qianfan"))
    url = str(getattr(searching_model, "url", "") or "")
    model = str(getattr(searching_model, "default_model", "") or "")
    if name == "qianfan":
        return QianfanBackend(api_key=api_key, url=url, model=model or "ernie-4.5-turbo-128k", client=client)
    if name == "searxng":
        return SearxngBackend(api_key=api_key, url=url, client=client)
    if name == "openai_hosted":
        return HostedSearchBackend(api_key=api_key, base_url=url or "https://api.openai.com/v1", model=model or "gpt-4o-mini", client=client)
    return ApiSearchBackend(name=name, api_key=api_key, url=url, client=client)
