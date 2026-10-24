"""M33 测试：小灶与课内追问共用同一套方案（气泡 + 块渲染 + 同一套工具）。

三件事必须成立：

1. 小灶的历史区是**气泡**，并且把 NDJSON 解析成内容块——不再把原始 JSON 糊在文本框里；
2. 流式过程增量渲染（未收完的一行原地刷新，收完的那一行换成正式块）；
3. 小灶也能拿到与学习台同一套工具（``executor`` / ``tools`` 透传给 Agent）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel

from app.agents.qa import QaAgent
from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.qa_service import QaService
from app.ui.pages.qa_page import QaPage

pytestmark = pytest.mark.no_network

GOOD_SVG = (
    '<svg viewBox="0 0 200 100">'
    "<title>函数图像</title><desc>斜线演示</desc>"
    '<path d="M 10 90 L 190 10" stroke="currentColor" stroke-width="2" fill="none"/>'
    "</svg>"
)


class _FakeClient:
    """按 33 字符切片吐字，故意把 JSON 行切断，逼出「增量解析」这条路径。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def stream_chat(self, messages, **kwargs):
        for index in range(0, len(self._text), 33):
            yield self._text[index : index + 33]


def _ndjson(*blocks: dict) -> str:
    return "".join(json.dumps(block, ensure_ascii=False) + "\n" for block in blocks)


def _page(tmp_path: Path, answer: str = "", **kwargs) -> tuple[QaPage, Path]:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course = tmp_path / "Courses" / "ATM 使用指南"
    page = QaPage(
        courses_dir=tmp_path / "Courses",
        agent_factory=lambda: QaAgent(_FakeClient(answer)),
        **kwargs,
    )
    return page, course


def _new_session(page: QaPage, qtbot, monkeypatch, title: str = "答疑会话") -> None:
    # 「新建会话」会弹模态输入框：测试里必须替掉，否则会一直等用户输入
    monkeypatch.setattr(QaPage, "_prompt_title", staticmethod(lambda: (title, True)))
    page.refresh()
    page.new_session_button.click()
    qtbot.waitUntil(lambda: page.session_list.count() == 1, timeout=3000)


# --- 气泡与块渲染 ---------------------------------------------------------
@pytest.mark.ui
def test_qa_page_renders_blocks_as_bubbles(qtbot, tmp_path: Path, monkeypatch) -> None:
    answer = _ndjson(
        {"type": "meta", "lesson_id": "L1-1", "depth": "standard", "version": 1},
        {"type": "text", "md": "因为两边变换要能接上。"},
        {"type": "svg", "title": "函数图像", "svg": GOOD_SVG},
        {"type": "done", "tokens": 0},
    )
    page, _ = _page(tmp_path, answer)
    qtbot.addWidget(page)
    _new_session(page, qtbot, monkeypatch)

    page.input_edit.setPlainText("这两处是同一回事吗？")
    page.ask_button.click()
    qtbot.waitUntil(lambda: "因为两边变换要能接上" in page.history.toPlainText(), timeout=20000)

    text = page.history.toPlainText()
    assert '{"type"' not in text, "小灶不该把原始 NDJSON 当正文显示"
    assert page.history._bubbles[0].role == "user"  # noqa: SLF001 - 测试直接看气泡序列
    assert page.history._bubbles[-1].role == "assistant"  # noqa: SLF001
    assert "因为两边变换要能接上" in page.history._bubbles[-1].markdown  # noqa: SLF001


@pytest.mark.ui
def test_qa_page_draws_svg_block_inside_bubble(qtbot, tmp_path: Path, monkeypatch) -> None:
    """图必须真的画出来（而不是留一段 SVG 源码）。"""
    answer = _ndjson({"type": "svg", "title": "函数图像", "svg": GOOD_SVG}, {"type": "done", "tokens": 0})
    page, _ = _page(tmp_path, answer)
    qtbot.addWidget(page)
    _new_session(page, qtbot, monkeypatch)

    page.input_edit.setPlainText("画一下")
    page.ask_button.click()
    qtbot.waitUntil(
        lambda: any(
            widget.objectName() == "ChatBubbleImage" and not widget.pixmap().isNull()
            for widget in page.history.findChildren(QLabel)
        ),
        timeout=20000,
    )


# --- 流式增量 -------------------------------------------------------------
@pytest.mark.ui
def test_qa_page_streams_partial_line_then_blocks(qtbot, tmp_path: Path) -> None:
    page, _ = _page(tmp_path)
    qtbot.addWidget(page)
    page.refresh()

    page._begin_answer()  # noqa: SLF001 - 直接驱动流式钩子，避免线程时序抖动
    bubble = page._current_bubble  # noqa: SLF001
    assert bubble is not None

    page._on_chunk('{"type":"text","md":"第一句')  # noqa: SLF001
    partial = [item for item in bubble.findChildren(QLabel) if item.property("role") == "muted"]
    assert partial, "未收完的一行要能看到（半句也要流出来）"
    assert "第一句" in partial[0].text()

    page._on_chunk('。第二句"}\n')  # noqa: SLF001
    page._on_done(None)  # noqa: SLF001

    assert "第一句。第二句" in bubble.markdown
    # 临时行是 deleteLater 掉的，给事件循环一个机会再断言
    qtbot.waitUntil(
        lambda: not [item for item in bubble.findChildren(QLabel) if item.property("role") == "muted"],
        timeout=2000,
    )


# --- 历史回看 -------------------------------------------------------------
@pytest.mark.ui
def test_qa_page_reload_parses_stored_ndjson(qtbot, tmp_path: Path) -> None:
    page, course = _page(tmp_path)
    qtbot.addWidget(page)

    service = QaService(session_path=course / "qa" / "sessions.jsonl", course_id="course-seed-atm-guide")
    session = service.create_session("历史会话")
    service.add_message(session.id, role="user", text="问题一")
    service.add_message(
        session.id,
        role="assistant",
        text=_ndjson({"type": "text", "md": "历史回答"}, {"type": "done", "tokens": 0}),
    )

    page.refresh()
    qtbot.waitUntil(lambda: page.session_list.count() == 1, timeout=3000)
    page.session_list.setCurrentRow(0)

    text = page.history.toPlainText()
    assert "问题一" in text and "历史回答" in text
    assert '{"type"' not in text


# --- 工具接线 -------------------------------------------------------------
@pytest.mark.ui
def test_qa_page_passes_tooling_to_agent(qtbot, tmp_path: Path, monkeypatch) -> None:
    sentinel_executor = object()
    sentinel_tools = [
        {"type": "function", "function": {"name": "plot_function", "description": "", "parameters": {}}}
    ]
    page, _ = _page(
        tmp_path,
        "",
        tooling_factory=lambda _course_dir: (sentinel_executor, sentinel_tools),
    )
    qtbot.addWidget(page)
    captured: dict = {}

    def fake_ask(self, session_id, question, **kwargs):
        captured.update(kwargs)

        async def generator():
            yield _ndjson({"type": "text", "md": "好"}, {"type": "done", "tokens": 0})

        return generator()

    monkeypatch.setattr(QaService, "ask", fake_ask)
    _new_session(page, qtbot, monkeypatch)

    page.input_edit.setPlainText("画一下 y=x^2")
    page.ask_button.click()
    qtbot.waitUntil(lambda: bool(captured), timeout=10000)

    assert captured["executor"] is sentinel_executor
    assert captured["tools"] == sentinel_tools


def test_course_tooling_is_none_without_client(tmp_path: Path) -> None:
    """没有模型客户端时退回纯对话，而不是抛异常。"""
    from app.agents.tooling import build_course_tooling

    executor, tools = build_course_tooling(course_dir=tmp_path, client=None)

    assert executor is None and tools is None
