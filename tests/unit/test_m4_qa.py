"""M4 测试：小灶（会话 / 引用限本课程 / 答疑 / 沉淀为笔记）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.qa import QaAgent
from app.bootstrap import bootstrap_workspace
from app.core.models import RefCard
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services import lesson_service
from app.services.qa_service import CrossCourseRef, QaService
from app.ui.pages.qa_page import QaPage

SEED_LESSON = Path("Courses") / "ATM 使用指南" / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


class _FakeClient:
    def __init__(self, text: str) -> None:
        self._text = text
        self.prompts: list[str] = []

    async def stream_chat(self, messages, **kwargs):
        self.prompts.append("\n".join(str(item.get("content", "")) for item in messages))
        for index in range(0, len(self._text), 33):
            yield self._text[index : index + 33]


def _service(tmp_path: Path, response: str = "") -> tuple[QaService, Path, Path]:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course = tmp_path / "Courses" / "ATM 使用指南"
    agent = QaAgent(_FakeClient(response)) if response else None
    service = QaService(
        session_path=course / "qa" / "sessions.jsonl",
        course_id="course-seed-atm-guide",
        agent=agent,
    )
    return service, course, tmp_path / SEED_LESSON


def test_session_and_ref_lifecycle(tmp_path: Path) -> None:
    service, course, lesson = _service(tmp_path)

    session = service.create_session("矩阵乘法与神经网络")
    ref_id = service.add_ref(session.id, RefCard(type="lesson_text", lesson_id="L1-1", excerpt="矩阵乘法不是逐项相乘"))

    sessions = service.sessions()
    assert sessions[0].id == session.id
    assert sessions[0].course_id == "course-seed-atm-guide"
    assert service.refs(session.id)[0]["id"] == ref_id

    records = service.load()
    assert [item["kind"] for item in records] == ["session", "ref"]


def test_cross_course_ref_is_rejected(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    session = service.create_session("跨课程测试")

    with pytest.raises(CrossCourseRef):
        service.add_ref(
            session.id,
            RefCard(type="lesson_text", lesson_id="L2-1", excerpt="别的课程的内容"),
            ref_course_id="course-other",
        )
    assert service.refs(session.id) == []


def test_ask_streams_and_records_messages(tmp_path: Path) -> None:
    answer = json.dumps({"type": "text", "md": "是同一回事，从两个角度描述。"}, ensure_ascii=False) + "\n" + json.dumps({"type": "done", "tokens": 0}) + "\n"
    service, _, _ = _service(tmp_path, answer)
    session = service.create_session("答疑")
    service.add_ref(session.id, RefCard(type="lesson_text", lesson_id="L1-1", excerpt="矩阵乘法"))

    async def run() -> list[str]:
        return [
            piece
            async for piece in service.ask(
                session.id, "这两处讲的矩阵乘法是同一回事吗？", refs=service.refs(session.id), outline_summary="C1 三分钟上手"
            )
        ]

    pieces = asyncio.run(run())
    messages = service.messages(session.id)

    assert "".join(pieces)
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["refs"]
    assert messages[1]["blocks"] == 2  # text + done
    assert "矩阵乘法" in service.agent.client.prompts[0]


def test_save_to_notes_for_course_and_lesson(tmp_path: Path) -> None:
    answer = json.dumps({"type": "text", "md": "结论：是同一回事。"}, ensure_ascii=False) + "\n"
    service, course, lesson = _service(tmp_path, answer)
    session = service.create_session("沉淀测试")
    asyncio.run_collect = None  # 避免误用

    async def run() -> None:
        async for _ in service.ask(session.id, "为什么？"):
            pass

    asyncio.run(run())

    course_note = service.save_to_notes(session.id, target="course")
    assert course_note == course / "notes" / "小灶汇总.md"
    assert "沉淀测试" in course_note.read_text(encoding="utf-8")

    lesson_note = service.save_to_notes(session.id, target="lesson", lesson_dir=lesson)
    assert lesson_note == lesson / "notebook.md"
    assert "老师" in lesson_note.read_text(encoding="utf-8")


@pytest.mark.ui
def test_qa_page_creates_session_adds_ref_and_asks(qtbot, tmp_path: Path, monkeypatch) -> None:
    answer = json.dumps({"type": "text", "md": "因为两边变换要能接上。"}, ensure_ascii=False) + "\n"
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    page = QaPage(courses_dir=tmp_path / "Courses", agent_factory=lambda: QaAgent(_FakeClient(answer)))
    qtbot.addWidget(page)

    monkeypatch.setattr(QaPage, "_prompt_title", staticmethod(lambda: ("答疑会话", True)))
    page.refresh()
    assert page.course_combo.count() == 1

    page.new_session_button.click()
    qtbot.waitUntil(lambda: page.session_list.count() == 1, timeout=3000)

    page.ref_excerpt_edit.setText("矩阵乘法不是逐项相乘")
    page.add_ref_button.click()
    assert page.ref_list.count() == 1

    page.input_edit.setPlainText("这两处是同一回事吗？")
    page.ask_button.click()
    qtbot.waitUntil(lambda: "因为两边变换要能接上" in page.history.toPlainText(), timeout=20000)

    page.save_course_note_button.click()
    assert (tmp_path / "Courses" / "ATM 使用指南" / "notes" / "小灶汇总.md").exists()
