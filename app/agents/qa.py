"""答疑顾问 Agent（《项目文档》§4.16 FR-16 小灶、《技术文档》§2.5）。

作用域**严格限定在单门课程内**：引用卡片只能来自本课程，跨课程引用由存储层直接拒绝。

与教师 Agent 的关键区别：答疑**只回答学生问的那一个问题**，使用
``prompts/qa/skeleton.md``（禁止复述讲义、禁止讲义五段式），学生要求画图时
必须真的输出 ``diagram`` 块。块协议与教师共用 ``prompts/teacher/protocol.md``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .base import AgentBase, PromptAssembler

DEPTHS = ("minimal", "standard", "deep")


class QaAgent(AgentBase):
    name = "qa"
    skeleton_prompt = "qa/skeleton.md"
    protocol_prompt = "teacher/protocol.md"
    default_task = "teach"

    def __init__(self, client: Any, *, assembler: PromptAssembler | None = None) -> None:
        super().__init__(client, assembler=assembler)

    def system_prompt(  # noqa: D401 - 与教师同形
        self, *, persona_body: str | None = None, depth: str = "standard", **overrides: Any
    ) -> str:
        depth_name = depth if depth in DEPTHS else "standard"
        return super().system_prompt(
            persona=persona_body, depth=f"teacher/depth_{depth_name}.md", **overrides
        )

    def build_user_prompt(
        self,
        *,
        question: str,
        refs: Sequence[Mapping[str, Any]] = (),
        outline_summary: str = "",
        history: Sequence[Mapping[str, Any]] = (),
        persona_name: str | None = None,
        selection: str = "",
    ) -> str:
        lines = [
            "这是课内答疑（小灶）。**学生的问题就是本次唯一的任务**：直接回答它，",
            "不要复述讲义、不要重讲本节、不要输出「钩子/直觉/拆解/样例/检验」那套结构。",
        ]
        if persona_name:
            lines.append(f"当前人设：{persona_name}")
        if outline_summary:
            lines.append("所处位置：" + outline_summary[:800])
        if selection:
            lines.append("学生选中的讲义片段：" + str(selection)[:800])
        if refs:
            lines.append("\n【可依据的引用卡片（不得超出本课程）】")
            for index, ref in enumerate(refs, start=1):
                label = ref.get("title") or ref.get("lesson_id") or ref.get("url") or ref.get("material_id")
                lines.append(
                    f"[{index}] {label}（{ref.get('type')}）：{str(ref.get('excerpt') or '')[:300]}"
                )
        else:
            lines.append("（本次没有引用卡片，只能基于课程大纲回答，并说明依据不足）")
        if history:
            lines.append("\n【历史答疑（最近若干条，仅供承接，不要重复）】")
            for record in history[-6:]:
                role = "学生" if record.get("role") == "user" else "你"
                lines.append(f"{role}：{str(record.get('text') or record.get('md') or '')[:200]}")
        lines.append(f"\n学生的问题：{question}")
        lines.append(
            "输出要求：NDJSON（首行 meta、末行 done）。先给结论，再给依据；"
            "学生要求「画一下 / 思维导图 / 流程」时必须真的输出 diagram 块（kind=svg 或 mermaid）。"
        )
        return "\n".join(lines)

    async def answer(
        self,
        *,
        question: str,
        refs: Sequence[Mapping[str, Any]] = (),
        outline_summary: str = "",
        history: Sequence[Mapping[str, Any]] = (),
        persona_name: str | None = None,
        selection: str = "",
        persona_body: str | None = None,
        depth: str = "standard",
        cancel: Any = None,
        executor: Any = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        lesson_id: str | None = None,
        course_id: str | None = None,
        **extra: Any,
    ) -> AsyncIterator[str]:
        """回答一个课内问题；带工具时走 Tool Loop（画图 / 检索资料等）。"""
        system = self.system_prompt(persona_body=persona_body, depth=depth)
        messages = [
            {
                "role": "user",
                "content": self.build_user_prompt(
                    question=question,
                    refs=refs,
                    outline_summary=outline_summary,
                    history=history,
                    persona_name=persona_name,
                    selection=selection,
                ),
            }
        ]
        kwargs: dict[str, Any] = {
            "task": "teach",
            "model": model,
            "temperature": temperature,
            "lesson_id": lesson_id,
            "course_id": course_id,
            "agent": self.name,
            **extra,
        }
        if executor is not None and tools:
            async for piece in self.run_tool_loop(
                messages, tools, executor=executor, system=system, cancel=cancel, **kwargs
            ):
                yield piece
            return
        async for piece in self.stream(messages, system=system, cancel=cancel, **kwargs):
            yield piece