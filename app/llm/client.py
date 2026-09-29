"""统一 LLM 客户端（《技术文档》§4.4.1、《项目文档》§13.1）。

**所有模型调用只经过这个出口**：档案切换、重试、流式、用量统计、取消。

重试语义：按《技术文档》§4.4.1 的示例代码 ``for attempt in range(self.max_retries)``，
``max_attempts = 3`` 表示**最多 3 次尝试**；退避间隔 ``backoff_base * 2**(n-1)``（上限 8s）；
401 / 403 立即上报、不重试。

一条补充约定（与 §4.10 失败矩阵一致）：**一旦已经开始输出内容就不再整段重试**，
否则用户会看到重复文本；此时直接抛错，由 UI 提供「续写」入口。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from ..core.cancel import CancellationToken
from ..core.config import AppConfig, LLMProfile
from ..core.errors import AuthError, ConfigError, FatalError, ProviderError, classify_exception
from ..core.routing import ModelRouter
from ..infra.secret_store import SecretStore, resolve_api_key, resolve_base_url
from .usage import UsageTracker

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
_CANCELLED = object()


@dataclass
class ConnectionReport:
    """「测试连接」的结果（FR-02 / AC-04：耗时 + 模型列表 + 错误原因）。"""

    ok: bool
    elapsed_ms: int = 0
    models: list[str] = field(default_factory=list)
    message: str = ""
    error: str | None = None
    status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "models": list(self.models),
            "message": self.message,
            "error": self.error,
            "status": self.status,
        }


class LLMClient:
    """OpenAI 兼容端点的唯一客户端。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        secret_store: SecretStore | None = None,
        usage: UsageTracker | None = None,
        client_factory: Callable[[LLMProfile, str], Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._router = ModelRouter(config)
        self._secrets = secret_store if secret_store is not None else SecretStore()
        self.usage = usage if usage is not None else UsageTracker()
        self._client_factory = client_factory or self._default_client_factory
        self._sleep = sleep or asyncio.sleep
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = float(backoff_base)
        self._env = env

    # --- 对外 API -------------------------------------------------------
    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        task: str = "teach",
        profile: LLMProfile | str | None = None,
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        json_schema: Mapping[str, Any] | None = None,
        cancel: CancellationToken | None = None,
        course_id: str | None = None,
        lesson_id: str | None = None,
        agent: str | None = None,
        web_search_backend: str | None = None,
        temperature: float | None = None,
        tool_sink: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """流式对话：逐段产出增量文本；``tool_sink`` 收集模型请求的工具调用（§4.5）。"""
        resolved = self._resolve_profile(task, profile, web_search_backend)
        api_key = self._require_api_key(resolved)
        model_name = model or resolved.chat_model
        token = cancel if cancel is not None else CancellationToken.never()

        for attempt in range(1, self.max_attempts + 1):
            if token.cancelled:
                return
            emitted = False
            try:
                async for piece in self._stream_once(
                    resolved=resolved,
                    api_key=api_key,
                    model_name=model_name,
                    messages=list(messages),
                    tools=tools,
                    json_schema=json_schema,
                    token=token,
                    temperature=temperature,
                    tool_sink=tool_sink,
                    course_id=course_id,
                    lesson_id=lesson_id,
                    agent=agent,
                    task=task,
                ):
                    emitted = True
                    yield piece
                return
            except ProviderError as exc:
                # 422/400 多为「某个可选字段不被端点接受」：用最小请求体自动重试一次（只试一次）                 if not emitted and (exc.status or 0) // 100 == 4 and attempt == 1:                     try:                         async for piece in self._stream_once(                             resolved=resolved, api_key=api_key, model_name=model_name,                             messages=list(messages), tools=None, json_schema=None, token=token,                             temperature=None, tool_sink=tool_sink, course_id=course_id,                             lesson_id=lesson_id, agent=agent, task=task,                         ):                             emitted = True                             yield piece                     except ProviderError:                         pass                     else:                         return
                if not exc.retryable or emitted or attempt >= self.max_attempts:
                    raise
                delay = min(self.backoff_base * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
                await self._sleep(delay)

    async def probe_connection(
        self,
        *,
        profile: LLMProfile | str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        task: str = "teach",
    ) -> ConnectionReport:
        """握手探测（设置页「测试连接」）：耗时 / 模型列表 / 错误原因。"""
        try:
            resolved = self._resolve_profile(task, profile, None)
        except ProviderError as exc:
            return ConnectionReport(ok=False, message=exc.explain(), error=str(exc), status=exc.status)

        key = (api_key or "").strip() or (
            resolve_api_key(resolved, self._secrets, env=self._env) or ""
        )
        if not key:
            return ConnectionReport(ok=False, message="请先填写 API Key", error="缺少 API Key")

        target = resolved
        if (base_url and base_url.strip()) or (model and model.strip()):
            updates: dict[str, Any] = {}
            if base_url and base_url.strip():
                updates["base_url"] = base_url.strip()
            if model and model.strip():
                updates["chat_model"] = model.strip()
            target = resolved.model_copy(update=updates)

        started = time.perf_counter()
        models_unsupported = False
        models: list[str] = []
        try:
            async with self._open(target, key) as client:
                try:
                    page = await client.models.list()
                    raw_items = getattr(page, "data", None) or []
                    models = [str(getattr(item, "id", "") or "") for item in raw_items]
                    models = [name for name in models if name]
                except Exception as exc:  # noqa: BLE001 - 端点差异较大，统一分类
                    classified = classify_exception(exc)
                    if isinstance(classified, AuthError):
                        raise classified from exc
                    models_unsupported = True
        except Exception as exc:  # noqa: BLE001
            classified = classify_exception(exc)
            return ConnectionReport(
                ok=False,
                elapsed_ms=_ms_since(started),
                error=str(classified),
                status=classified.status,
                message=f"{classified.explain()}（{classified.status or type(exc).__name__}）",
            )

        elapsed = _ms_since(started)
        if models_unsupported:
            return ConnectionReport(
                ok=True, elapsed_ms=elapsed, models=[], message="握手成功；该端点未提供模型列表接口"
            )
        message = f"连接成功，可用模型 {len(models)} 个" if models else "连接成功"
        return ConnectionReport(ok=True, elapsed_ms=elapsed, models=models, message=message)


    async def review_image(
        self,
        image_bytes: bytes,
        *,
        prompt: str,
        task: str = "vision_review",
        model: str | None = None,
        cancel: CancellationToken | None = None,
        max_bytes: int = 6 * 1024 * 1024,
    ) -> str:
        """多模态读图（默认档案 DeepSeek 自带读图，无需额外配置，AC-76）。"""
        if not image_bytes:
            raise FatalError("没有可评审的图片数据")
        if len(image_bytes) > max_bytes:
            raise FatalError("图片过大，已跳过多模态评审")
        import base64

        data = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
                ],
            }
        ]
        parts: list[str] = []
        async for piece in self.stream_chat(messages, task=task, model=model, cancel=cancel):
            parts.append(piece)
        return "".join(parts)
    # --- 内部 -----------------------------------------------------------
    def _resolve_profile(
        self,
        task: str,
        profile: LLMProfile | str | None,
        web_search_backend: str | None,
    ) -> LLMProfile:
        if isinstance(profile, LLMProfile):
            return profile
        if isinstance(profile, str):
            found = self._config.profile(profile)
            if found is None:
                raise ConfigError(f"找不到模型档案：{profile}")
            return found
        resolved = self._router.profile_for(task, web_search_backend=web_search_backend)
        if resolved is None:
            raise ConfigError(f"任务 {task!r} 没有可用的模型档案")
        return resolved

    def _require_api_key(self, profile: LLMProfile) -> str:
        key = resolve_api_key(profile, self._secrets, env=self._env)
        if not key:
            raise AuthError(f"档案 {profile.name} 没有可用的 API Key，请到设置页配置")
        return key

    def _default_client_factory(self, profile: LLMProfile, api_key: str) -> Any:
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": resolve_base_url(profile, env=self._env),
            "timeout": profile.timeout_seconds,
            # 重试由本客户端统一负责（§4.4.1），SDK 自带重试必须关闭
            "max_retries": 0,
        }
        if profile.proxy:
            import httpx

            kwargs["http_client"] = httpx.AsyncClient(
                proxy=profile.proxy, timeout=profile.timeout_seconds
            )
        return AsyncOpenAI(**kwargs)

    @asynccontextmanager
    async def _open(self, profile: LLMProfile, api_key: str) -> AsyncIterator[Any]:
        client = self._client_factory(profile, api_key)
        try:
            yield client
        finally:
            await _maybe_aclose(client)

    async def _stream_once(
        self,
        *,
        resolved: LLMProfile,
        api_key: str,
        model_name: str,
        messages: list[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        json_schema: Mapping[str, Any] | None,
        token: CancellationToken,
        temperature: float | None,
        tool_sink: list[dict[str, Any]] | None,
        course_id: str | None,
        lesson_id: str | None,
        agent: str | None,
        task: str,
    ) -> AsyncIterator[str]:
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "temperature": resolved.temperature if temperature is None else float(temperature),
        }
        if resolved.max_tokens:
            kwargs["max_tokens"] = resolved.max_tokens
        if tools:
            kwargs["tools"] = list(tools)
        if json_schema:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": dict(json_schema)}

        reported_usage = False
        try:
            async with self._open(resolved, api_key) as client:
                try:
                    stream = await client.chat.completions.create(**kwargs)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - 统一翻译成项目异常
                    raise classify_exception(exc) from exc

                loop = asyncio.get_running_loop()
                cancelled = asyncio.Event()
                tool_collector = _ToolCallCollector()
                unsubscribe = token.on_cancel(lambda: loop.call_soon_threadsafe(cancelled.set))
                if token.cancelled:
                    cancelled.set()
                try:
                    iterator = stream.__aiter__()
                    while True:
                        try:
                            chunk = await _anext_or_cancel(iterator, cancelled)
                        except StopAsyncIteration:
                            break
                        except asyncio.CancelledError:
                            raise
                        except BaseException as exc:  # noqa: BLE001 - 流中途异常同样要分类
                            raise classify_exception(exc) from exc
                        if chunk is _CANCELLED:
                            await _maybe_aclose(stream)
                            return

                        usage = getattr(chunk, "usage", None)
                        if usage is not None:
                            reported_usage = True
                            self.usage.record(
                                model=model_name,
                                prompt_tokens=_int_attr(usage, "prompt_tokens"),
                                completion_tokens=_int_attr(usage, "completion_tokens"),
                                course_id=course_id,
                                lesson_id=lesson_id,
                                agent=agent,
                                task=task,
                            )

                        tool_collector.feed(chunk)
                        text = _extract_text(chunk)
                        if text:
                            yield text
                    if tool_sink is not None:
                        tool_sink.extend(tool_collector.finish())
                finally:
                    unsubscribe()
        finally:
            if not reported_usage:
                # 端点未上报 usage 时也记一次调用（便于预算与成本可视化）
                self.usage.record(
                    model=model_name,
                    prompt_tokens=0,
                    completion_tokens=0,
                    course_id=course_id,
                    lesson_id=lesson_id,
                    agent=agent,
                    task=task,
                )


class _ToolCallCollector:
    """把流式分片的 ``tool_calls`` 合并成完整调用（OpenAI 兼容协议按 index 分片下发）。"""

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, Any]] = {}

    def feed(self, chunk: Any) -> None:
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            for call in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(call, "index", 0) or 0)
                slot = self._by_index.setdefault(index, {"id": "", "name": "", "arguments": ""})
                call_id = getattr(call, "id", None)
                if call_id:
                    slot["id"] = str(call_id)
                function = getattr(call, "function", None)
                if function is not None:
                    name = getattr(function, "name", None)
                    if name:
                        slot["name"] = str(name)
                    arguments = getattr(function, "arguments", None)
                    if arguments:
                        slot["arguments"] += str(arguments)

    def finish(self) -> list[dict[str, Any]]:
        return [self._by_index[index] for index in sorted(self._by_index)]


async def _anext_or_cancel(iterator: AsyncIterator[Any], cancelled: asyncio.Event) -> Any:
    """取下一个流片段；若期间收到取消，立即返回 ``_CANCELLED``。"""
    next_task = asyncio.ensure_future(iterator.__anext__())
    cancel_task = asyncio.ensure_future(cancelled.wait())
    try:
        done, _ = await asyncio.wait({next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if next_task in done:
            return next_task.result()
        next_task.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await next_task
        return _CANCELLED
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task


async def _maybe_aclose(target: Any) -> None:
    closer = getattr(target, "aclose", None) or getattr(target, "close", None)
    if closer is None:
        return
    with suppress(Exception):
        result = closer()
        if inspect.isawaitable(result):
            await result


def _extract_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None)
    return "" if content is None else str(content)


def _int_attr(target: Any, name: str) -> int:
    try:
        return max(0, int(getattr(target, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
