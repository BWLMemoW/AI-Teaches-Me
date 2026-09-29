"""M23 测试：追问回答渲染 Markdown（不再吐原始 NDJSON）、右栏宽度、放大窗口。"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _ndjson(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def test_answer_renders_markdown_not_raw_ndjson(qtbot) -> None:
    """回归：追问回答曾把原始 NDJSON（{"type":...}）吐到界面上。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)

    drawer.begin_answer()
    drawer.feed_answer_piece(_ndjson({"type": "text", "md": "## 重点\n\n- 第一条\n- 第二条"}))
    drawer.feed_answer_piece(_ndjson({"type": "code", "lang": "python", "code": "print(1)"}))
    drawer.end_answer()

    plain = drawer.history.toPlainText()
    assert '{"type"' not in plain
    assert '"md"' not in plain
    assert "重点" in plain and "第一条" in plain
    assert "print(1)" in plain


def test_history_records_are_rendered(qtbot) -> None:
    """历史记录（落盘可能是 NDJSON）加载后也要渲染成可读文本。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.load_records(
        [
            {"role": "user", "text": "为什么？"},
            {"role": "assistant", "md": _ndjson({"type": "text", "md": "因为 **如此**"})},
        ]
    )
    plain = drawer.history.toPlainText()
    assert "我：为什么？" in plain
    assert '{"type"' not in plain
    assert "因为" in plain and "如此" in plain


def test_drawer_is_wide_enough_for_reading(qtbot) -> None:
    """右栏太窄渲染难看：追问抽屉最小宽度 >= 380。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    assert drawer.minimumWidth() >= 380


def test_zoom_button_requests_zoom(qtbot) -> None:
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.show()
    with qtbot.waitSignal(drawer.zoom_requested, timeout=3000):
        drawer.zoom_button.click()


def _build_study_page(qtbot, tmp_path):
    from PySide6.QtWidgets import QWidget

    from app.bootstrap import Services, bootstrap_workspace
    from app.infra.secret_store import InMemoryBackend, SecretStore
    from app.infra.sqlite_index import SqliteIndex
    from app.services import course_service, lesson_service
    from app.services.library_service import LibraryService
    from app.services.outline_service import OutlineService
    from app.services.settings_service import SettingsService
    from app.ui.pages.study_page import StudyPage

    class _Renderer:
        def __init__(self) -> None:
            self._widget = QWidget()

        def widget(self):
            return self._widget

        def load_page(self, path): ...

        def set_theme(self, css): ...

        def render_blocks(self, blocks): ...

        def append_block(self, block): ...

        def render_markdown(self, text): ...

    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    index = SqliteIndex(tmp_path / "data" / "library.db")
    services = Services(
        result=result,
        index=index,
        client=object(),
        settings=SettingsService(
            result.config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())
        ),
        library=LibraryService(courses_dir=result.layout.courses, index=index),
        outline=OutlineService(courses_dir=result.layout.courses, client=object(), index=index),
        course=course_service,
        lesson=lesson_service,
    )
    page = StudyPage(services=services, renderer=_Renderer())
    qtbot.addWidget(page)
    return page, services


def test_study_page_zoom_opens_document_window(qtbot, tmp_path, monkeypatch) -> None:
    from app.ui.widgets.document_viewer import DocumentViewerDialog

    page, services = _build_study_page(qtbot, tmp_path)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")
        page.chat_drawer.append_user("这是什么？")
        assert page.chat_drawer.conversation_markdown().strip()

        opened: list[str] = []
        monkeypatch.setattr(
            DocumentViewerDialog,
            "exec",
            lambda self, *a, **k: opened.append(self.viewer.toMarkdown()),
        )
        page._open_chat_window()
        assert opened and "这是什么？" in opened[0]
    finally:
        services.index.close()

def test_conversation_markdown_keeps_structure(qtbot) -> None:
    """「放大」窗口读的是 Markdown 源：结构（标题/列表/代码围栏）必须保留。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.append_user("两个问题")
    drawer.begin_answer()
    drawer.feed_answer_piece(_ndjson({"type": "text", "md": "### 小标题\n\n- 甲\n- 乙"}))
    drawer.feed_answer_piece(_ndjson({"type": "code", "lang": "python", "code": "x = 1"}))
    drawer.end_answer()

    md = drawer.conversation_markdown()
    assert "**我：** 两个问题" in md
    assert "**老师：**" in md
    assert "### 小标题" in md and "- 甲" in md
    assert "python" in md and "x = 1" in md
    assert '{"type"' not in md