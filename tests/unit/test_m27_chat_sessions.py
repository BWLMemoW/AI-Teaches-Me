"""M27 测试：追问会话（每个小节多会话、一键新建、互不串上下文）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services import lesson_service

pytestmark = pytest.mark.ui  # 会话逻辑是纯服务层，但保持与其他 M 测试一致


def _lesson(tmp_path: Path) -> Path:
    lesson = tmp_path / "L1-1"
    lesson.mkdir(parents=True, exist_ok=True)
    return lesson


# --- 服务层 ---------------------------------------------------------------
def test_create_sessions_and_isolate_messages(tmp_path: Path) -> None:
    lesson = _lesson(tmp_path)
    first = lesson_service.create_chat_session(lesson, title="会话一")
    second = lesson_service.create_chat_session(lesson, title="会话二")

    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "A 问题"}, session_id=first["id"])
    lesson_service.append_chat_turn(lesson, {"role": "assistant", "md": "A 回答"}, session_id=first["id"])
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "B 问题"}, session_id=second["id"])

    sessions = lesson_service.chat_sessions(lesson)
    assert [item["title"] for item in sessions] == ["会话一", "会话二"]
    assert lesson_service.latest_chat_session_id(lesson) == second["id"]

    a_messages = lesson_service.load_chat(lesson, session_id=first["id"])
    assert [item.get("text") or item.get("md") for item in a_messages] == ["A 问题", "A 回答"]
    b_messages = lesson_service.load_chat(lesson, session_id=second["id"])
    assert [item["text"] for item in b_messages] == ["B 问题"]
    # 不带 session_id 时仍然能拿到全部消息（笔记 / 导出用）
    assert len(lesson_service.load_chat(lesson)) == 3


def test_legacy_session_reads_back_new_turns(tmp_path: Path) -> None:
    """M35 回归：老记录（没有 session 字段）合成「历史追问」后，新回答会被写成 session=chat-legacy。

    以前 ``load_chat`` 只认「没有 session 字段」的老记录，于是新回答一落盘就读不回来——
    界面上表现为「追问第二次，答案消失了」（每次回答结束都会重载记录，所以必现）。
    """
    lesson = _lesson(tmp_path)
    # 老版本留下的两行：没有 session 字段（M27 之前的格式）
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "老问题"})
    lesson_service.append_chat_turn(lesson, {"role": "assistant", "md": "老回答"})

    legacy = lesson_service.latest_chat_session_id(lesson)
    assert legacy == lesson_service.LEGACY_SESSION_ID

    # 新版追问写进这个虚拟会话：读写必须对称
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "新问题一"}, session_id=legacy)
    lesson_service.append_chat_turn(lesson, {"role": "assistant", "md": "新回答一"}, session_id=legacy)
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "新问题二"}, session_id=legacy)
    lesson_service.append_chat_turn(lesson, {"role": "assistant", "md": "新回答二"}, session_id=legacy)

    messages = lesson_service.load_chat(lesson, session_id=legacy)
    assert [item.get("text") or item.get("md") for item in messages] == [
        "老问题", "老回答", "新问题一", "新回答一", "新问题二", "新回答二",
    ]
    # 只合成一个「历史追问」会话，不会因为新记录多出一个会话
    assert [item["id"] for item in lesson_service.chat_sessions(lesson)] == [legacy]


def test_ask_twice_in_legacy_session_keeps_both_answers(tmp_path: Path) -> None:
    """按界面的真实顺序复现：在「历史追问」里连问两次，两次问答都要读得回来。"""

    class _FakeQa:
        def __init__(self) -> None:
            self.calls = 0

        async def answer(self, **_kwargs):
            self.calls += 1
            for block in (
                {"type": "text", "md": f"第 {self.calls} 次回答"},
                {"type": "done", "tokens": 0},
            ):
                yield json.dumps(block, ensure_ascii=False) + "\n"

    lesson = _lesson(tmp_path)
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "老问题"})
    session_id = lesson_service.latest_chat_session_id(lesson)
    qa = _FakeQa()

    async def ask(question: str) -> None:
        async for _piece in lesson_service.ask_question(
            lesson, teacher=None, qa=qa, question=question, context={}, session_id=session_id
        ):
            pass

    asyncio.run(ask("第一次提问"))
    asyncio.run(ask("第二次提问"))

    texts = [
        str(item.get("text") or item.get("md") or "")
        for item in lesson_service.load_chat(lesson, session_id=session_id)
    ]
    assert any("第一次提问" in text for text in texts)
    assert any("第 1 次回答" in text for text in texts)
    assert any("第二次提问" in text for text in texts)
    assert any("第 2 次回答" in text for text in texts), "第二次的回答必须读得回来（否则界面表现为消失）"


def test_first_question_auto_names_the_session(tmp_path: Path) -> None:
    lesson = _lesson(tmp_path)
    session = lesson_service.create_chat_session(lesson)
    assert session["title"] == lesson_service.DEFAULT_SESSION_TITLE

    lesson_service.append_chat_turn(
        lesson, {"role": "user", "text": "为什么行数等于列数才能相乘？"}, session_id=session["id"]
    )

    assert lesson_service.chat_sessions(lesson)[0]["title"].startswith("为什么行数等于列数")


def test_legacy_records_become_history_session(tmp_path: Path) -> None:
    lesson = _lesson(tmp_path)
    path = lesson / "chat.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (
                {"ts": "2026-01-01T00:00:00+08:00", "role": "user", "text": "老问题"},
                {"ts": "2026-01-01T00:00:01+08:00", "role": "assistant", "md": "老回答"},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = lesson_service.chat_sessions(lesson)

    assert len(sessions) == 1
    assert sessions[0]["id"] == lesson_service.LEGACY_SESSION_ID
    assert [item.get("text") or item.get("md") for item in lesson_service.load_chat(lesson, session_id=sessions[0]["id"])] == [
        "老问题",
        "老回答",
    ]


def test_delete_keeps_file_append_only(tmp_path: Path) -> None:
    lesson = _lesson(tmp_path)
    session = lesson_service.create_chat_session(lesson, title="待删")
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "一句"}, session_id=session["id"])
    before_lines = (lesson / "chat.jsonl").read_text(encoding="utf-8").strip().splitlines()

    lesson_service.delete_chat_session(lesson, session["id"])

    after_lines = (lesson / "chat.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(after_lines) == len(before_lines) + 1  # 只追加了一行删除标记
    assert lesson_service.chat_sessions(lesson) == []
    assert lesson_service.load_chat(lesson, session_id=session["id"]) == []


def test_rename_is_append_only_and_wins(tmp_path: Path) -> None:
    lesson = _lesson(tmp_path)
    session = lesson_service.create_chat_session(lesson, title="旧名")
    lesson_service.rename_chat_session(lesson, session["id"], "新名")

    sessions = lesson_service.chat_sessions(lesson)

    assert len(sessions) == 1
    assert sessions[0]["title"] == "新名"


def test_ensure_chat_session_creates_once(tmp_path: Path) -> None:
    lesson = _lesson(tmp_path)
    first = lesson_service.ensure_chat_session(lesson)
    second = lesson_service.ensure_chat_session(lesson)

    assert first["id"] == second["id"]
    assert len(lesson_service.chat_sessions(lesson)) == 1


def test_ask_question_writes_into_the_given_session(tmp_path: Path) -> None:
    """追问必须写进指定会话，且只带上本会话的历史。"""
    import asyncio

    from app.agents.qa import QaAgent

    class _RecordingClient:
        def __init__(self) -> None:
            self.rounds: list[list[dict]] = []

        async def stream_chat(self, messages, **kwargs):
            self.rounds.append([dict(item) for item in messages])
            yield '{"type":"text","md":"答案"}\n'

    lesson = _lesson(tmp_path)
    first = lesson_service.create_chat_session(lesson, title="旧会话")
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "旧问题"}, session_id=first["id"])
    second = lesson_service.create_chat_session(lesson, title="新会话")

    client = _RecordingClient()

    async def run() -> None:
        async for _ in lesson_service.ask_question(
            lesson,
            teacher=None,
            qa=QaAgent(client),
            question="新问题",
            context={"lesson": {"id": "L1-1", "title": "t"}},
            session_id=second["id"],
        ):
            pass

    asyncio.run(run())

    prompt = client.rounds[0][1]["content"]
    assert "新问题" in prompt
    assert "旧问题" not in prompt  # 新会话不该看到旧会话的历史

    new_messages = lesson_service.load_chat(lesson, session_id=second["id"])
    assert new_messages[0]["text"] == "新问题"
    assert new_messages[1]["role"] == "assistant"
    assert "答案" in new_messages[1]["md"]  # 助手侧落盘的是 NDJSON 原文，界面负责渲染
    old_messages = lesson_service.load_chat(lesson, session_id=first["id"])
    assert [item["text"] for item in old_messages] == ["旧问题"]

# --- 界面：一键新建 / 切换 / 重命名 / 删除 ---------------------------------
def test_study_page_new_session_button_creates_and_switches(qtbot, tmp_path: Path) -> None:
    from app.ui.widgets.diagram_dialog import DiagramDialog  # noqa: F401 - 确保 ui 包已就绪
    from tests.unit.test_m23_chat_render import _build_study_page

    page, services = _build_study_page(qtbot, tmp_path)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")
        lesson = page._lesson_dir
        assert lesson is not None
        assert page.chat_drawer.session_combo.count() == 1  # 打开小节即有一个会话
        first_id = page.chat_drawer.current_session_id()
        assert first_id

        page.chat_drawer.new_session_button.click()  # 一键新建
        assert page.chat_drawer.session_combo.count() == 2
        new_id = page.chat_drawer.current_session_id()
        assert new_id and new_id != first_id

        lesson_service.append_chat_turn(
            lesson, {"role": "user", "text": "新会话专属"}, session_id=new_id
        )
        page._on_chat_session_changed(new_id)
        assert "新会话专属" in page.chat_drawer.history.toPlainText()

        page._on_chat_session_changed(first_id)
        assert "新会话专属" not in page.chat_drawer.history.toPlainText()
    finally:
        services.index.close()


def test_rename_and_delete_session_from_page(qtbot, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from tests.unit.test_m23_chat_render import _build_study_page

    page, services = _build_study_page(qtbot, tmp_path)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")
        lesson = page._lesson_dir
        session_id = page.chat_drawer.current_session_id()

        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("系统策划追问", True))
        page._on_rename_chat_session(session_id)
        assert page.chat_drawer.session_combo.currentText() == "系统策划追问"

        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        page._on_delete_chat_session(session_id)
        assert page._on_chat_session_changed  # 方法存在
        ids = {item["id"] for item in lesson_service.chat_sessions(lesson)}
        assert session_id not in ids
        assert page.chat_drawer.session_combo.count() == 1  # 删完自动补一个新会话
    finally:
        services.index.close()
