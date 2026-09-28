"""小灶服务（《项目文档》§4.16、《技术文档》§3.6 ``qa/sessions.jsonl``）。

三种记录混写在同一个文件，靠 ``kind`` 区分：``session`` / ``message`` / ``ref``。
**所有 ref 的课程必须与所在会话一致**，跨课程引用直接拒绝（AC-56 / T-29）。
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.models import RefCard
from ..infra import file_repository
from ..llm.stream import parse_blocks
from . import notes_service


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    return f"qa-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(2)}"


class CrossCourseRef(ValueError):
    """跨课程引用（存储层拒绝，T-29）。"""


@dataclass
class QaSession:
    id: str
    course_id: str
    title: str
    created_at: str
    messages: int = 0
    refs: int = 0


class QaService:
    def __init__(self, *, session_path: str | Path, course_id: str, agent: Any = None) -> None:
        self.path = Path(session_path)
        self.course_id = course_id
        self.agent = agent

    # --- 读 -------------------------------------------------------------
    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        import json

        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def sessions(self) -> list[QaSession]:
        found: dict[str, QaSession] = {}
        for record in self.load():
            if record.get("kind") == "session":
                found[str(record["id"])] = QaSession(
                    id=str(record["id"]),
                    course_id=str(record.get("course_id") or self.course_id),
                    title=str(record.get("title") or "未命名会话"),
                    created_at=str(record.get("created_at") or ""),
                )
            elif record.get("kind") == "message":
                session = found.get(str(record.get("session_id")))
                if session is not None:
                    session.messages += 1
            elif record.get("kind") == "ref":
                session_id = str(record.get("session_id") or "")
                session = found.get(session_id)
                if session is not None:
                    session.refs += 1
        return sorted(found.values(), key=lambda item: item.created_at, reverse=True)

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.load()
            if record.get("kind") == "message" and record.get("session_id") == session_id
        ]

    def refs(self, session_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.load()
            if record.get("kind") == "ref" and record.get("session_id") == session_id
        ]

    # --- 写 -------------------------------------------------------------
    def create_session(self, title: str, *, persona: str | None = None) -> QaSession:
        session = QaSession(
            id=new_session_id(), course_id=self.course_id, title=title or "未命名会话", created_at=now_iso()
        )
        file_repository.append_record(
            self.path,
            {
                "kind": "session",
                "id": session.id,
                "course_id": self.course_id,
                "title": session.title,
                "persona": persona,
                "created_at": session.created_at,
            },
        )
        return session

    def add_ref(self, session_id: str, ref: RefCard | dict[str, Any], *, ref_course_id: str | None = None) -> str:
        """添加引用卡片；``ref_course_id`` 与会话课程不一致时直接拒绝（AC-56）。"""
        payload = ref.model_dump(exclude_none=True) if isinstance(ref, RefCard) else dict(ref)
        if ref_course_id and ref_course_id != self.course_id:
            raise CrossCourseRef("小灶只能引用本课程的内容")
        ref_id = str(payload.get("id") or f"ref-{secrets.token_hex(3)}")
        record = {**payload, "kind": "ref", "session_id": session_id, "id": ref_id}  # 权威字段放最后，避免被 payload 的空值覆盖
        record.setdefault("course_id", self.course_id)
        file_repository.append_record(self.path, record)
        return ref_id

    def add_message(self, session_id: str, *, role: str, text: str, refs: Sequence[str] = (), blocks: int = 0, tokens: int = 0) -> None:
        file_repository.append_record(
            self.path,
            {
                "kind": "message",
                "session_id": session_id,
                "ts": now_iso(),
                "role": role,
                "text": text if role == "user" else "",
                "md": "" if role == "user" else text,
                "refs": list(refs),
                "blocks": blocks,
                "tokens": tokens,
            },
        )

    # --- 答疑 -----------------------------------------------------------
    async def ask(
        self,
        session_id: str,
        question: str,
        *,
        refs: Sequence[dict[str, Any]] = (),
        outline_summary: str = "",
        persona_name: str | None = None,
        cancel: Any = None,
    ) -> AsyncIterator[str]:
        history = self.messages(session_id)
        self.add_message(session_id, role="user", text=question, refs=[str(item.get("id")) for item in refs])
        collected: list[str] = []
        async for piece in self.agent.answer(
            question=question,
            refs=list(refs),
            outline_summary=outline_summary,
            history=history,
            persona_name=persona_name,
        ):
            collected.append(piece)
            yield piece
        text = "".join(collected)
        blocks, _ = parse_blocks(text)
        self.add_message(session_id, role="assistant", text=text, blocks=len(blocks), tokens=len(text) // 4)

    # --- 沉淀 -----------------------------------------------------------
    def save_to_notes(self, session_id: str, *, target: str = "course", lesson_dir: str | Path | None = None) -> Path:
        """把答疑沉淀为笔记：课程级 ``notes/小灶汇总.md`` 或指定小节的 ``notebook.md``（AC-41）。"""
        session = next((item for item in self.sessions() if item.id == session_id), None)
        title = session.title if session else "小灶"
        parts = [f"## {title}", f"（沉淀时间：{now_iso()}）", ""]
        for record in self.load():
            if record.get("kind") == "message" and record.get("session_id") == session_id:
                role = "我" if record.get("role") == "user" else "老师"
                text = str(record.get("text") or record.get("md") or "").strip()
                parts.append(f"- **{role}**：{text[:800]}")
        body = "\n".join(parts) + "\n"
        if target == "lesson" and lesson_dir is not None:
            destination = Path(lesson_dir) / "notebook.md"
            existing = file_repository.read_text_or_empty(destination)
            file_repository.write_text(destination, existing.rstrip() + "\n\n" + body)
            return destination
        destination = self.path.parents[1] / "notes" / "小灶汇总.md"
        existing = file_repository.read_text_or_empty(destination)
        file_repository.write_text(destination, existing.rstrip() + "\n\n" + body)
        return destination
