"""Agent 基类（《技术文档》§4.4.2 提示词装配、§4.5 / §13.13 Tool Loop）。

职责边界：

* 装配系统提示词（固定顺序 + 分层夹逼 + 人设越权剔除）；
* 提供 Tool Loop（循环在本文件、注册在 ``tools.py``、执行在 ``infra/tool_executor.py``）；
* **不做**网络细节（交给 ``app/llm/client.py``），**不做**事务与落盘（交给 services）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core.errors import ConfigError

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"
MAX_TOOL_ROUNDS = 3
UNTRUSTED_TEMPLATE = '<untrusted source="{source}">{body}</untrusted>'

# 人设越权模式（§4.4.2 / §13.9）：**必须同时命中「动作 + 目标」才剔除该行**。
# 早期版本只匹配单个词（如「省略」「输出」），把人设里正常的风格句（例如
# 「不要省略术语的准确解释」）也误删了，导致「人设看起来没生效」。
PERSONA_CONFLICT_PATTERNS: tuple[tuple[str, ...], ...] = (
    ("不要", "协议"), ("不要", "输出格式"), ("不要", "json"), ("不要", "五段"),
    ("忽略", "协议"), ("忽略", "规则"), ("忽略", "以上"), ("忽略", "上面"),
    ("省略", "知识点"), ("减少", "知识点"), ("压缩", "知识点"), ("跳过", "知识点"),
    ("不讲", "知识点"), ("只讲", "重点"), ("讲浅", "知识点"),
    ("ignore", "protocol"), ("ignore", "previous"), ("ignore", "above"),
    ("skip", "protocol"),
)


class PromptMissing(ConfigError):
    user_message = "提示词文件缺失"


class ToolExecutorProtocol(Protocol):
    async def run(self, call: Mapping[str, Any], cancel: Any = None) -> Any: ...


@dataclass(frozen=True)
class PromptParts:
    """一次装配需要的提示词片段（顺序即《技术文档》§4.4.2 的固定顺序）。"""

    shared: tuple[str, ...] = ("shared/safety.md", "shared/untrusted.md", "shared/style.md")
    skeleton: str = ""
    persona: str | None = None
    depth: str | None = None
    protocol: str = ""
    coverage: str | None = "shared/coverage.md"
    extra: tuple[str, ...] = ()


class PromptAssembler:
    """提示词装配器：分层夹逼 + 人设越权检测。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else PROMPTS_ROOT

    def load(self, relative: str) -> str:
        path = self.root / relative
        if not path.exists():
            raise PromptMissing(f"缺少提示词文件：{relative}")
        return path.read_text(encoding="utf-8").strip()

    def filter_persona(self, body: str) -> tuple[str, list[str]]:
        """剔除与硬边界冲突的人设行，返回（保留正文, 被剔除的行）。"""
        kept: list[str] = []
        dropped: list[str] = []
        for line in (body or "").splitlines():
            lowered = line.lower()
            if any(
                all(token in lowered for token in group) for group in PERSONA_CONFLICT_PATTERNS
            ):
                dropped.append(line.strip())
                continue
            kept.append(line)
        return "\n".join(kept).strip(), dropped

    def assemble(self, parts: PromptParts) -> str:
        chunks: list[str] = [self.load(name) for name in parts.shared]
        if parts.skeleton:
            chunks.append(self.load(parts.skeleton))
        if parts.persona:
            body, dropped = self.filter_persona(parts.persona)
            if body:
                chunks.append("## 当前教学人设（只影响表达层）\n\n" + body)
            if dropped:
                chunks.append(f"（已忽略 {len(dropped)} 条与教学规则冲突的人设指令）")
        if parts.depth:
            chunks.append(self.load(parts.depth))
        if parts.protocol:
            chunks.append(self.load(parts.protocol))
        if parts.coverage:
            chunks.append(self.load(parts.coverage))
        chunks.extend(self.load(name) for name in parts.extra)
        return "\n\n---\n\n".join(chunk for chunk in chunks if chunk)


def wrap_untrusted(result: Any, *, source: str = "tool") -> str:
    """把工具 / 外部结果包装成不可信内容（§4.5 第 3 步、§6 安全边界）。"""
    if isinstance(result, str):
        body = result
    else:
        body = json.dumps(result, ensure_ascii=False, default=str)
    return UNTRUSTED_TEMPLATE.format(source=source, body=body)



def normalize_tool_calls(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把内部收集到的工具调用规范成 OpenAI / DeepSeek 要求的消息结构。

    内部形状（``app/llm/client.py`` 的 ``_ToolCallCollector``）：``{"id","name","arguments"}``；
    协议要求：``{"id","type":"function","function":{"name","arguments"}}``，其中 arguments 必须是
    **JSON 字符串**。缺了 type/function 包装会被端点判为非法请求（实测 DeepSeek 返回 422）。
    """
    normalized: list[dict[str, Any]] = []
    for call in calls:
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
        normalized.append(
            {
                "id": str(call.get("id") or f"call_{len(normalized)}"),
                "type": "function",
                "function": {"name": str(call.get("name") or ""), "arguments": arguments or "{}"},
            }
        )
    return normalized

class AgentBase:
    """所有 Agent 的基类。"""

    name = "agent"
    skeleton_prompt = ""
    protocol_prompt = ""
    default_task = "teach"

    def __init__(
        self,
        client: Any,
        *,
        assembler: PromptAssembler | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.client = client
        self.assembler = assembler or PromptAssembler()
        self.max_tool_rounds = max_tool_rounds

    # --- 提示词 ---------------------------------------------------------
    def system_prompt(self, *, persona: str | None = None, depth: str | None = None, **overrides: Any) -> str:
        parts = PromptParts(
            skeleton=self.skeleton_prompt,
            persona=persona,
            depth=depth,
            protocol=self.protocol_prompt,
            **overrides,
        )
        return self.assembler.assemble(parts)

    def with_system(
        self,
        messages: Sequence[Mapping[str, Any]],
        system: str | None,
    ) -> list[dict[str, Any]]:
        conversation: list[dict[str, Any]] = [dict(item) for item in messages]
        if system:
            conversation.insert(0, {"role": "system", "content": system})
        return conversation

    # --- 调用 -----------------------------------------------------------
    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        system: str | None = None,
        task: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        conversation = self.with_system(messages, system)
        async for piece in self.client.stream_chat(conversation, task=task or self.default_task, **kwargs):
            yield piece

    async def run_tool_loop(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        executor: ToolExecutorProtocol,
        system: str | None = None,
        cancel: Any = None,
        task: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """§4.5 的 Tool Loop：内容块照常外发，工具结果以不可信内容回喂，最多 3 轮。"""
        conversation = self.with_system(messages, system)
        for _round in range(self.max_tool_rounds + 1):
            tool_calls: list[dict[str, Any]] = []
            async for piece in self.client.stream_chat(
                conversation,
                task=task or self.default_task,
                tools=list(tools) if tools else None,
                cancel=cancel,
                tool_sink=tool_calls,
                **kwargs,
            ):
                yield piece

            if not tool_calls:
                return

            # 回喂给模型时必须是**标准 OpenAI 结构**：否则 DeepSeek 等端点会直接 422
            # {"role":"assistant","tool_calls":[{"id":..,"type":"function",
            #   "function":{"name":..,"arguments":"<JSON 字符串>"}}]}
            conversation.append({"role": "assistant", "tool_calls": normalize_tool_calls(tool_calls)})
            for call in tool_calls:
                result = await executor.run(call, cancel)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": wrap_untrusted(result, source=str(call.get("name") or "tool")),
                    }
                )
