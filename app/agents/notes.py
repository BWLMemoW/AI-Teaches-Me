"""笔记整理 Agent（《项目文档》§8.1 / §13.11、§9.11 模板）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .base import AgentBase, PromptAssembler


class NoteKeeperAgent(AgentBase):
    name = "notekeeper"
    skeleton_prompt = "notes/skeleton.md"
    protocol_prompt = "notes/protocol.md"
    default_task = "teach"

    def __init__(self, client: Any, *, assembler: PromptAssembler | None = None) -> None:
        super().__init__(client, assembler=assembler)

    def build_user_prompt(
        self,
        *,
        lesson_id: str,
        lesson_title: str,
        objectives: Sequence[str] = (),
        knowledge_points: Sequence[Mapping[str, Any]] = (),
        script_markdown: str = "",
        chat_records: Sequence[Mapping[str, Any]] = (),
        notebook: str = "",
        mastery: int | None = None,
        incomplete: bool = False,
    ) -> str:
        lines = [
            f"小节：{lesson_id} {lesson_title}",
            "学习目标：" + ("；".join(objectives) if objectives else "（未给出）"),
            "知识点清单：",
        ]
        for point in knowledge_points:
            lines.append(f"- {point.get('title', '')}：{point.get('summary', '')}")
        if mastery:
            lines.append(f"学生自评：{mastery}（1 没懂 / 2 一般 / 3 懂了）")
        if incomplete:
            lines.append("注意：本小节尚未学完，请在标题下标注。")
        lines.append("\n【讲义正文】\n" + script_markdown[:12000])
        if chat_records:
            lines.append("\n【追问记录】")
            for record in chat_records:
                role = "学生" if record.get("role") == "user" else "老师"
                text = str(record.get("text") or record.get("md") or "")
                lines.append(f"{role}：{text[:600]}")
        if notebook.strip():
            lines.append("\n【学生手写笔记】\n" + notebook[:4000])
        lines.append("\n请按模板输出这一节的结构化 Markdown 笔记。")
        return "\n".join(lines)

    async def make_notes(self, **payload: Any) -> AsyncIterator[str]:
        user_prompt = self.build_user_prompt(**payload)
        async for piece in self.stream(
            [{"role": "user", "content": user_prompt}], task="teach", agent=self.name
        ):
            yield piece
