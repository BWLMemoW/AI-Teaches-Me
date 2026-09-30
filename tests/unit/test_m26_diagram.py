"""M26 测试：追问流的增量渲染（不闪）+ 双击图看整张 + 讲义多图。"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QLabel

pytestmark = pytest.mark.ui

SVG_SAMPLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 120" width="320" height="120">'
    '<rect width="320" height="120" fill="#EAF3FF"/>'
    '<text x="16" y="40" font-size="18" fill="#1F3A5F">思维导图</text>'
    "</svg>"
)


def _ndjson(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


# --- 增量渲染（不再一闪一闪） ---------------------------------------------
def test_streaming_appends_without_rebuilding_widgets(qtbot) -> None:
    """回归：以前每来一个块就 clear + 重建整个气泡 → 界面反复横跳、闪烁。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    drawer.feed_answer_piece(_ndjson({"type": "text", "md": "第一块"}))
    bubble = drawer.history._bubbles[0]

    before = {id(widget) for widget in bubble.findChildren(QLabel)}
    assert before

    drawer.feed_answer_piece(_ndjson({"type": "text", "md": "第二块"}))
    qtbot.wait(30)  # 让「若被重建」的 deleteLater 生效

    after = {id(widget) for widget in bubble.findChildren(QLabel)}
    assert before <= after, "已有控件不应被销毁重建（这正是闪烁的来源）"
    assert len(after) > len(before)
    plain = drawer.history.toPlainText()
    assert "第一块" in plain and "第二块" in plain


def test_partial_line_updates_in_place(qtbot) -> None:
    """未收完的那一行只在末尾临时标签里原地刷新，收完即转正。"""
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    bubble = drawer.history._bubbles[0]

    drawer.feed_answer_piece('{"type":"text","md":"逐字')
    partial = bubble._partial_label
    assert partial is not None
    assert "逐字" in partial.text()
    assert len(bubble.html_parts) == 0  # 还只是预览，不算正式块

    drawer.feed_answer_piece('测试"}\n')
    assert bubble._partial_label is partial  # 同一个控件，没有被重建
    assert "逐字测试" in drawer.history.toPlainText()

    drawer.end_answer()
    assert bubble._partial_label is None  # 收尾时临时行被摘掉


# --- 双击看图 -------------------------------------------------------------
def test_double_click_image_requests_full_view(qtbot) -> None:
    from app.ui.widgets.chat_drawer import ChatDrawer

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.begin_answer()
    drawer.feed_answer_piece(
        _ndjson({"type": "diagram", "kind": "svg", "title": "思维导图", "content": SVG_SAMPLE})
    )
    drawer.end_answer()

    images = [
        label
        for label in drawer.history._bubbles[0].findChildren(QLabel)
        if label.pixmap() is not None and not label.pixmap().isNull()
    ]
    assert images

    with qtbot.waitSignal(drawer.diagram_requested, timeout=3000) as blocker:
        images[0].doubleClicked.emit()

    payload = blocker.args[0]
    assert "<svg" in payload["svg"]
    assert payload["title"] == "思维导图"


def test_diagram_dialog_renders_svg(qtbot) -> None:
    from app.ui.widgets.diagram_dialog import DiagramDialog

    dialog = DiagramDialog(svg=SVG_SAMPLE, title="思维导图")
    qtbot.addWidget(dialog)
    assert dialog.loaded() is True
    assert dialog.windowTitle() == "思维导图"

    dialog.zoom_by(2.0)
    dialog.fit_to_window()
    dialog.show_actual_size()


def test_diagram_dialog_rejects_blank_svg(qtbot) -> None:
    from app.ui.widgets.diagram_dialog import DiagramDialog

    dialog = DiagramDialog(svg="", title="空的")
    qtbot.addWidget(dialog)
    assert dialog.loaded() is False


def test_study_page_opens_diagram_window(qtbot, tmp_path, monkeypatch) -> None:
    from app.ui.widgets.diagram_dialog import DiagramDialog
    from tests.unit.test_m23_chat_render import _build_study_page

    page, services = _build_study_page(qtbot, tmp_path)
    try:
        opened: list[DiagramDialog] = []
        monkeypatch.setattr(DiagramDialog, "exec", lambda self, *a, **k: opened.append(self))
        page._open_diagram({"svg": SVG_SAMPLE, "title": "思维导图"})
        assert opened and opened[0].loaded() is True
    finally:
        services.index.close()


# --- 讲义要多画图/表 ------------------------------------------------------
def test_teacher_skeleton_requires_visuals() -> None:
    from pathlib import Path

    text = Path("prompts/teacher/skeleton.md").read_text(encoding="utf-8")
    assert "至少给出 1 张「可视化」" in text
    assert "不要用 mermaid" in text
    assert "kind` 必须是 `svg" in text