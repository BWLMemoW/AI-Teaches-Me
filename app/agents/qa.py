"""答疑顾问 Agent（《项目文档》§4.16 FR-16 小灶、《技术文档》§2.5）。

作用域**严格限定在单门课程内**：引用卡片只能来自本课程，跨课程引用由存储层直接拒绝。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .base import AgentBase, PromptAssembler


class QaAgent(AgentBase):
    name = "qa"
    skeleton_prompt = "teacher/skeleton.md"
    protocol_prompt = "teacher/protocol.md"
    default_task = "teach"

    def __init__(self, client: Any, *, assembler: PromptAssembler | None = None) -> None:
        super().__init__(client, assembler=assembler)

    def build_user_prompt(
        self,
        *,
        question: str,
        refs: Sequence[Mapping[str, Any]] = (),
        outline_summary: str = "",
        history: Sequence[Mapping[str, Any]] = (),
        persona_name: str | None = None,
    ) -> str:
        lines = ["这是课内答疑（小灶），只能使用下面这些本课程的引用卡片作为依据。"]
        if persona_name:
            lines.append(f"当前人设：{persona_name}")
        if outline_summary:
            lines.append("课程大纲摘要：\n" + outline_summary[:1500])
        if refs:
            lines.append("\n【引用卡片】")
            for index, ref in enumerate(refs, start=1):
                label = ref.get("title") or ref.get("lesson_id") or ref.get("url") or ref.get("material_id")
                lines.append(f"[{index}] {label}（{ref.get('type')}）：{str(ref.get('excerpt') or '')[:400]}")
        else:
            lines.append("（本次没有引用卡片，只能基于课程大纲回答，并说明依据不足）")
        if history:
            lines.append("\n【历史答疑（最近若干条）】")
            for record in history[-6:]:
                role = "学生" if record.get("role") == "user" else "你"
                lines.append(f"{role}：{str(record.get('text') or record.get('md') or '')[:300]}")
        lines.append(f"\n学生的问题：{question}")
        lines.append("请用 NDJSON 回答（text 块 + done 块）；引用时才用 ref 块，并标注来源编号。")
        return "\n".join(lines)

    async def answer(self, **payload: Any) -> AsyncIterator[str]:
        user_prompt = self.build_user_prompt(**payload)
        async for piece in self.stream(
            [{"role": "user", "content": user_prompt}], task="teach", agent=self.name
        ):
            yield piece
