"""工具执行器（《技术文档》§4.5 / §13.13：并行 / 超时 / 缓存 / 预算 / 清洗）。

约定（对应 §4.5 的约束表）：

* 单轮可并行调用多个工具，同一工具最多 3 个；
* 超时或失败时返回**结构化错误**，让模型基于已有信息继续，而不是中断整节讲义；
* 命中缓存不计入预算；每日预算与单小节次数上限在这里强制；
* 返回给模型的内容由 ``agents/base.py`` 包装成 ``<untrusted>``。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from collections.abc import Callable, Sequence


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的定义（名字 / 描述 / JSON Schema / 执行函数 / 超时）。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    timeout: float = 15.0
    cacheable: bool = False


class ToolRegistry:
    """工具注册表：注册在 ``agents/tools.py``，执行与预算在本模块（技术债 C-09）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, allowed: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """OpenAI 兼容的 tools 载荷；``allowed`` 是人设工具白名单的交集。"""
        selected = [name for name in self.names() if allowed is None or name in set(allowed)]
        return [
            {
                "type": "function",
                "function": {
                    "name": self._tools[name].name,
                    "description": self._tools[name].description,
                    "parameters": self._tools[name].parameters,
                },
            }
            for name in selected
        ]

MAX_PARALLEL_PER_TOOL = 3
DEFAULT_DAILY_LIMIT = 100


def now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ToolCallResult:
    name: str
    ok: bool
    data: Any = None
    error: str = ""
    message: str = ""
    cached: bool = False
    elapsed_ms: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool": self.name, "ok": self.ok}
        if self.ok:
            payload["result"] = self.data
        else:
            payload["error"] = self.error
            payload["message"] = self.message
        payload["cached"] = self.cached
        payload["elapsed_ms"] = self.elapsed_ms
        return payload


@dataclass
class CallBudget:
    """调用预算：默认每天 100 次、每次讲义每种工具最多 2 次（§4.19 成本控制）。"""

    daily_limit: int = DEFAULT_DAILY_LIMIT
    per_lesson_limits: dict[str, int] = field(default_factory=lambda: {"web_search": 2})
    daily_used: int = 0
    lesson_used: dict[str, int] = field(default_factory=dict)

    def check(self, name: str) -> tuple[bool, str]:
        if self.daily_limit is not None and self.daily_used >= self.daily_limit:
            return False, "已达到每日预算上限"
        limit = self.per_lesson_limits.get(name)
        if limit is not None and self.lesson_used.get(name, 0) >= limit:
            return False, f"本小节 {name} 调用次数已达上限（{limit} 次）"
        return True, ""

    def consume(self, name: str) -> None:
        self.daily_used += 1
        self.lesson_used[name] = self.lesson_used.get(name, 0) + 1


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        budget: CallBudget | None = None,
        cache: dict[str, tuple[float, Any]] | None = None,
        cache_ttl_seconds: float = 24 * 3600,
    ) -> None:
        self.registry = registry
        self.budget = budget
        self._cache = cache if cache is not None else {}
        self._cache_ttl = cache_ttl_seconds
        self.log: list[dict[str, Any]] = []

    # --- 单次执行 -------------------------------------------------------
    async def run(self, call: dict[str, Any], cancel: Any = None) -> dict[str, Any]:
        name = str(call.get("name") or "")
        spec = self.registry.get(name)
        if spec is None:
            return self._record(ToolCallResult(name=name, ok=False, error="unknown_tool", message="工具未注册"))

        if cancel is not None and getattr(cancel, "cancelled", False):
            return self._record(ToolCallResult(name=name, ok=False, error="cancelled", message="用户已取消"))

        arguments, error = self._parse_arguments(call.get("arguments"))
        if error:
            return self._record(ToolCallResult(name=name, ok=False, error="bad_arguments", message=error))
        missing = self._missing_required(spec, arguments)
        if missing:
            return self._record(
                ToolCallResult(name=name, ok=False, error="bad_arguments", message="缺少参数：" + "、".join(missing))
            )

        cache_key = self._cache_key(name, arguments)
        if spec.cacheable:
            hit = self._cache.get(cache_key)
            if hit is not None and (time.time() - hit[0]) < self._cache_ttl:
                return self._record(
                    ToolCallResult(name=name, ok=True, data=hit[1], cached=True), arguments=arguments
                )

        if self.budget is not None:
            allowed, reason = self.budget.check(name)
            if not allowed:
                return self._record(
                    ToolCallResult(name=name, ok=False, error="budget_exceeded", message=reason), arguments=arguments
                )

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(asyncio.to_thread(spec.handler, **arguments), timeout=spec.timeout)
        except asyncio.TimeoutError:
            if self.budget is not None:
                self.budget.consume(name)
            return self._record(
                ToolCallResult(
                    name=name,
                    ok=False,
                    error="timeout",
                    message=f"工具超过 {spec.timeout:.0f}s 未返回，已中断该轮",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                ),
                arguments=arguments,
            )
        except Exception as exc:  # noqa: BLE001 - 工具失败不能让讲义生成崩掉
            if self.budget is not None:
                self.budget.consume(name)
            return self._record(
                ToolCallResult(
                    name=name,
                    ok=False,
                    error="tool_failed",
                    message=str(exc),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                ),
                arguments=arguments,
            )

        if self.budget is not None:
            self.budget.consume(name)
        if spec.cacheable:
            self._cache[cache_key] = (time.time(), result)
        if isinstance(result, dict) and result.get("error"):
            return self._record(
                ToolCallResult(name=name, ok=False, error="tool_error", message=str(result.get("error"))),
                arguments=arguments,
            )
        return self._record(
            ToolCallResult(
                name=name,
                ok=True,
                data=result,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ),
            arguments=arguments,
        )

    # --- 同轮并行 -------------------------------------------------------
    async def run_many(self, calls: Sequence[dict[str, Any]], cancel: Any = None) -> list[dict[str, Any]]:
        """同一轮多个工具并行执行，但同一工具最多 3 个调用（§4.5 约束表）。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for call in calls:
            grouped.setdefault(str(call.get("name") or ""), []).append(call)

        tasks: list[asyncio.Task[dict[str, Any]]] = []
        for name, items in grouped.items():
            for call in items[:MAX_PARALLEL_PER_TOOL]:
                tasks.append(asyncio.ensure_future(self.run(call, cancel)))
            for extra in items[MAX_PARALLEL_PER_TOOL:]:
                tasks.append(
                    asyncio.ensure_future(
                        self._immediate(
                            str(extra.get("name") or ""), "too_many_calls", "同一轮同一工具最多 3 个调用"
                        )
                    )
                )
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))

    async def _immediate(self, name: str, error: str, message: str) -> dict[str, Any]:
        return self._record(ToolCallResult(name=name, ok=False, error=error, message=message))

    # --- 内部 -----------------------------------------------------------
    def _record(self, result: ToolCallResult, *, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.log.append(
            {
                "ts": now_iso(),
                "tool": result.name,
                "arguments": arguments or {},
                "ok": result.ok,
                "cached": result.cached,
                "elapsed_ms": result.elapsed_ms,
                "error": result.error,
            }
        )
        return result.to_payload()

    @staticmethod
    def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str]:
        if raw is None or raw == "":
            return {}, ""
        if isinstance(raw, dict):
            return raw, ""
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}, "工具参数不是合法 JSON"
        if not isinstance(parsed, dict):
            return {}, "工具参数必须是 JSON 对象"
        return parsed, ""

    @staticmethod
    def _missing_required(spec: ToolSpec, arguments: dict[str, Any]) -> list[str]:
        required = spec.parameters.get("required") or []
        return [
            key
            for key in required
            if key not in arguments or arguments[key] in ("", None)
        ]

    @staticmethod
    def _cache_key(name: str, arguments: dict[str, Any]) -> str:
        return name + ":" + json.dumps(arguments, ensure_ascii=False, sort_keys=True)


def summarize_log(entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    items = list(entries)
    return {
        "calls": len(items),
        "cached": sum(1 for item in items if item.get("cached")),
        "failed": sum(1 for item in items if not item.get("ok")),
    }
