"""M23 测试：追问气泡式对话（用户在右 / 老师在左）、Markdown 渲染、宽度与放大窗口。"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, Qt

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


def test_bubble_header_marks_streaming_only_while_running(qtbot) -> None:
    """M33 回归：表头只在流式回答期间挂「正在回答」，历史记录里的旧回答不该一直挂着。"""
    from app.ui.widgets.chat_drawer import ChatHistoryView

    view = ChatHistoryView()
    qtbot.addWidget(view)
    bubble = view.add_bubble("assistant", "历史回答")

    assert "老师" in bubble._header_label.text()  # noqa: SLF001 - 直接看表头文本
    assert "正在回答" not in bubble._header_label.text()  # noqa: SLF001

    bubble.set_partial("（正在思考…）")
    assert "正在回答" in bubble._header_label.text()  # noqa: SLF001

    bubble.finish_stream()
    assert "正在回答" not in bubble._header_label.text()  # noqa: SLF001


def test_rich_elements_are_real_html(qtbot) -> None:
    """表格/代码块必须真的是富文本元素（而不是被当成纯文本原样显示）。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    drawer.feed_answer_piece(
        _ndjson({"type": "table", "headers": ["步骤", "做什么"], "rows": [["1", "对齐基"]]})
    )
    drawer.feed_answer_piece(_ndjson({"type": "code", "lang": "python", "code": "x = 1"}))
    drawer.end_answer()

    html = "\n".join(drawer.history._bubbles[0].html_parts)
    assert "<table" in html
    assert "对齐基" in html
    assert "monospace" in html  # 代码块内联了等宽字体
    plain = drawer.history.toPlainText()
    assert "对齐基" in plain and "x = 1" in plain

def test_streaming_updates_one_bubble(qtbot) -> None:
    """块级流式：多个内容块应落在同一个老师气泡里，而不是每块新建气泡。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    drawer.feed_answer_piece(_ndjson({"type": "text", "md": "第一块"}))
    drawer.feed_answer_piece(_ndjson({"type": "summary", "points": ["要点一"]}))
    assert len(drawer.history._bubbles) == 1
    plain = drawer.history.toPlainText()
    assert "第一块" in plain and "要点一" in plain


def test_user_bubble_is_right_and_teacher_left(qtbot) -> None:
    """主人要的布局：用户气泡靠右，老师气泡靠左。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.resize(460, 560)
    drawer.show()
    drawer.append_user("短问题")
    drawer.begin_answer()
    drawer.feed_answer_piece(_ndjson({"type": "text", "md": "回答"}))
    drawer.end_answer()

    bubbles = drawer.history._bubbles
    assert [bubble.role for bubble in bubbles] == ["user", "assistant"]
    qtbot.wait(80)
    host = drawer.history.widget()
    user_x = bubbles[0].mapTo(host, QPoint(0, 0)).x()
    teacher_x = bubbles[1].mapTo(host, QPoint(0, 0)).x()
    assert user_x > teacher_x
    assert teacher_x <= 12  # 老师气泡贴左边（只剩容器 10px 外边距）
    assert bubbles[0].maximumWidth() >= 200


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
    assert "老师：" in md
    assert "### 小标题" in md and "- 甲" in md
    assert "python" in md and "x = 1" in md
    assert '{"type"' not in md


def test_drawer_is_wide_enough_for_reading(qtbot) -> None:
    """右栏太窄气泡换行难看：追问抽屉最小宽度 >= 380。"""
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

SVG_SAMPLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 120" width="320" height="120">'
    '<rect width="320" height="120" fill="#EAF3FF"/>'
    '<text x="16" y="40" font-size="18" fill="#1F3A5F">思维导图</text>'
    '<text x="16" y="80" font-size="14" fill="#1F3A5F">岗位定位 / 协作 / 产出</text>'
    "</svg>"
)


def test_svg_block_is_drawn_as_image(qtbot) -> None:
    """回归：模型按要求画了图，但 diagram 块没有渲染分支 → 气泡里「图呢？」。"""
    from PySide6.QtWidgets import QLabel

    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    drawer.feed_answer_piece(
        _ndjson({"type": "diagram", "kind": "svg", "title": "思维导图", "content": SVG_SAMPLE})
    )
    drawer.end_answer()

    bubble = drawer.history._bubbles[0]
    pixmaps = [
        label.pixmap()
        for label in bubble.findChildren(QLabel)
        if label.pixmap() is not None and not label.pixmap().isNull()
    ]
    assert pixmaps, "SVG 块必须被真的画成图片"
    assert pixmaps[0].width() > 0 and pixmaps[0].height() > 0
    assert "图示" in bubble.to_plain_text()
    assert '{"type"' not in drawer.history.toPlainText()


def test_mermaid_block_shows_source_instead_of_disappearing(qtbot) -> None:
    """本项目没有离线 mermaid 渲染器：至少要把源码显示出来，不能整块消失。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    drawer.feed_answer_piece(
        _ndjson({"type": "diagram", "kind": "mermaid", "content": "graph TD\n  A-->B"})
    )
    drawer.end_answer()

    plain = drawer.history.toPlainText()
    assert "graph TD" in plain
    assert "A-->B" in plain
    assert "mermaid" in plain.lower()


# --- 自动滚动（M37：答案往下长时要跟到底部） -------------------------------
def test_history_auto_scrolls_while_streaming(qtbot) -> None:
    """回归：以前立刻取 ``maximum()`` 拿到的是布局前的旧值，视图会停在上面看不到新内容。"""
    from app.ui.widgets.chat_drawer import ChatHistoryView

    view = ChatHistoryView()
    view.resize(420, 200)
    view.show()
    qtbot.addWidget(view)
    bubble = view.add_bubble("assistant", "")
    for index in range(20):
        bubble.append_block({"type": "text", "md": f"第 {index} 段内容。" * 30})
        view.scroll_to_bottom()

    bar = view.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0 and bar.value() == bar.maximum(), timeout=3000)


def test_history_stops_sticking_when_user_scrolls_up(qtbot) -> None:
    from app.ui.widgets.chat_drawer import ChatHistoryView

    view = ChatHistoryView()
    view.resize(420, 200)
    view.show()
    qtbot.addWidget(view)
    bubble = view.add_bubble("assistant", "")
    for index in range(20):
        bubble.append_block({"type": "text", "md": f"第 {index} 段内容。" * 30})
    view.scroll_to_bottom()

    bar = view.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=3000)
    bar.setValue(0)  # 用户自己往上滚
    bubble.append_block({"type": "text", "md": "新增内容" * 80})
    qtbot.wait(100)

    assert bar.value() == 0, "用户往上滚之后不该被自动拽回底部"
