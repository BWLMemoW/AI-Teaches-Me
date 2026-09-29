"""联网搜索服务（《项目文档》§4.19 / §13.12；默认关闭 + 缓存 + 每日预算）。

两条调用路径共用本实现：Agent 工具调用（工具名 ``web_search``）与系统预取。
成本控制四件套：默认关闭、按需触发、24h 缓存、每日预算硬上限。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..infra import file_repository
from ..infra.web_search import SearchHit, build_backend, clean_hits

CACHE_SUBDIR = "web"
BUDGET_FILE = "budget.json"

# 时效敏感词表（只对这些内容触发联网，§4.19 成本控制第 2 条）
SENSITIVE_KEYWORDS = (
    "最新", "版本", "release", "价格", "收费", "政策", "法规", "标准",
    "api", "用法", "参数", "界面", "更新", "停用", "迁移", "标准库",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class SearchResult:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    cached: bool = False
    backend: str = ""
    stopped: bool = False
    message: str = ""

    def as_tool_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "cached": self.cached,
            "backend": self.backend,
            "stopped": self.stopped,
            "message": self.message,
            "results": [hit.to_dict() for hit in self.hits],
        }


class SearchService:
    def __init__(
        self,
        *,
        cache_dir: str | Path,
        searching_model: Any,
        api_key: str = "",
        backend: Any = None,
        client: Any = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) / CACHE_SUBDIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.model_config = searching_model
        self.api_key = api_key
        self._backend = backend if backend is not None else build_backend(searching_model, api_key=api_key, client=client)

    # --- 触发判定 -------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.model_config, "enabled", False))

    @staticmethod
    def should_trigger(text: str) -> bool:
        lowered = (text or "").lower()
        return any(keyword in lowered for keyword in SENSITIVE_KEYWORDS)

    # --- 预算 -----------------------------------------------------------
    @property
    def budget_file(self) -> Path:
        return self.cache_dir / BUDGET_FILE

    def budget_state(self) -> dict[str, Any]:
        data = file_repository.read_json_or_default(self.budget_file, {}) or {}
        today = datetime.now().date().isoformat()
        if data.get("date") != today:
            data = {"date": today, "used": 0}
        data.setdefault("used", 0)
        limit = int(getattr(self.model_config, "daily_call_budget", 100) or 100)
        return {"date": data["date"], "used": int(data["used"]), "limit": limit, "remaining": max(0, limit - int(data["used"]))}

    def _consume_budget(self, calls: int = 1) -> None:
        needs_key = str(getattr(self.model_config, "backend", "")) != "searxng"
        if needs_key and not self.api_key:
            return SearchResult(
                query=text,
                message="未配置搜索 API Key：请到 设置 → 联网与时效 填写搜索密钥",
                stopped=True,
            )

        state = self.budget_state()
        file_repository.write_json(
            self.budget_file, {"date": state["date"], "used": state["used"] + calls}
        )

    def reset_budget(self) -> None:
        file_repository.write_json(self.budget_file, {"date": datetime.now().date().isoformat(), "used": 0})

    # --- 缓存 -----------------------------------------------------------
    def _cache_path(self, query: str, k: int) -> Path:
        digest = hashlib.sha1(f"{query}|{k}".encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, query: str, k: int) -> list[SearchHit] | None:
        path = self._cache_path(query, k)
        data = file_repository.read_json_or_default(path, None)
        if not data:
            return None
        ttl_hours = int(getattr(self.model_config, "cache_ttl_hours", 24) or 24)
        try:
            saved_at = datetime.fromisoformat(str(data.get("ts")))
        except ValueError:
            return None
        age_hours = (datetime.now().astimezone() - saved_at).total_seconds() / 3600
        if age_hours > ttl_hours:
            return None
        return [SearchHit(**item) for item in data.get("hits", [])]

    def _write_cache(self, query: str, k: int, hits: list[SearchHit]) -> None:
        file_repository.write_json(
            self._cache_path(query, k),
            {"ts": now_iso(), "query": query, "k": k, "hits": [hit.to_dict() for hit in hits]},
        )

    # --- 查询 -----------------------------------------------------------
    async def search(self, query: str, k: int | None = None) -> SearchResult:
        size = int(k or getattr(self.model_config, "top_k", 5) or 5)
        text = (query or "").strip()
        if not text:
            return SearchResult(query=query, message="查询为空", stopped=True)
        if not self.enabled:
            return SearchResult(query=text, message="联网搜索未开启", stopped=True)

        cached = self._read_cache(text, size)
        if cached is not None:
            return SearchResult(query=text, hits=cached, cached=True, backend=getattr(self._backend, "name", ""))

        needs_key = str(getattr(self.model_config, "backend", "")) != "searxng"
        if needs_key and not self.api_key:
            return SearchResult(
                query=text,
                message="未配置搜索 API Key：请到 设置 → 联网与时效 填写搜索密钥",
                stopped=True,
            )

        state = self.budget_state()
        if state["remaining"] <= 0:
            return SearchResult(query=text, message="已达到每日预算上限", stopped=True)

        try:
            hits = await self._backend.search(text, size)
        except Exception as exc:  # noqa: BLE001 - 搜索失败不阻塞讲义
            return SearchResult(query=text, message=f"搜索失败：{exc}", stopped=True)

        hits = clean_hits([hit.to_dict() for hit in hits], k=size)
        self._consume_budget()
        self._write_cache(text, size, hits)
        return SearchResult(query=text, hits=hits, backend=getattr(self._backend, "name", ""))

    def search_sync(self, query: str, k: int | None = None) -> list[dict[str, str]]:
        """给工具执行器用的同步包装（在工作线程里跑）。"""
        import asyncio

        result = asyncio.run(self.search(query, k))
        return [hit.to_dict() for hit in result.hits]
