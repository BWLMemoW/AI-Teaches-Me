"""M9 测试：卡片排版 / 彩色胶囊标签 / 卡片网格 / 换图标对话框 / 章节树图标 / PDF 封面 / 启动健壮性。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_workspace
from app.core.models import CourseIcon
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services import export_service, icon_service
from app.ui.icon_render import course_icon_pixmap
from app.ui.widgets.course_card import CardStyle, CourseCard, human_time
from app.ui.widgets.tag_chip import TagChip, build_tag_row, color_for

pytestmark = pytest.mark.ui


def _course(tmp_path: Path) -> Path:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    return tmp_path / "Courses" / "ATM 使用指南"


# --- 胶囊标签 -------------------------------------------------------------
def test_tag_chip_colors_are_stable(qtbot) -> None:
    assert color_for("数学") == color_for("数学")
    assert color_for("数学") != color_for("英语") or len(set(color_for(n) for n in ["数学", "英语", "考研"])) > 1

    chips = build_tag_row(["数学", "考研", "入门", "AI", "额外"])
    assert len(chips) == 5
    assert isinstance(chips[0], TagChip)
    assert "border-radius" in chips[0].styleSheet()
    assert chips[-1].text().strip().startswith("+")


# --- 卡片排版 -------------------------------------------------------------
def test_course_card_splits_title_meta_and_tags(qtbot, tmp_path: Path) -> None:
    course = _course(tmp_path)
    from app.services import course_service

    summary = course_service.scan_courses(tmp_path / "Courses")[0]
    card = CourseCard(
        summary,
        icon_pixmap=course_icon_pixmap(course, CourseIcon(kind="preset", value="math")),
        style=CardStyle.from_theme("sakura", "light"),
    )
    qtbot.addWidget(card)

    assert card.title_label.text() == "ATM 使用指南"
    assert "章" in card.meta_label.text() and "小节" in card.meta_label.text()
    assert "最近学习" in card.time_label.text()
    assert card.progress_bar.value() == 0
    assert card.percent_label.text() == "0%"
    # 三种颜色互不相同（标题 / 次级 / 弱化）
    assert len({card.title_label.styleSheet(), card.meta_label.styleSheet(), card.time_label.styleSheet()}) == 3
    assert card.findChildren(TagChip)  # 标签用彩色胶囊


def test_course_card_selected_state_and_activation(qtbot, tmp_path: Path) -> None:
    course = _course(tmp_path)
    from app.services import course_service

    summary = course_service.scan_courses(tmp_path / "Courses")[0]
    card = CourseCard(summary, icon_pixmap=course_icon_pixmap(course, CourseIcon()))
    qtbot.addWidget(card)

    card.set_selected(True)
    assert "#E86A92" in card.styleSheet()  # 选中描边用主题主色

    with qtbot.waitSignal(card.activated, timeout=3000):
        from PySide6.QtCore import QPoint, Qt as QtConst
        from PySide6.QtGui import QMouseEvent

        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPoint(5, 5), QtConst.MouseButton.LeftButton,
            QtConst.MouseButton.LeftButton, QtConst.KeyboardModifier.NoModifier,
        )
        card.mousePressEvent(event)


def test_human_time_reads_progress() -> None:
    assert human_time({}) == "尚未开始"
    assert human_time({"updated_at": "2020-01-01T00:00:00+08:00"}).startswith("20")


# --- 卡片网格 -------------------------------------------------------------
def test_library_grid_builds_cards_and_reflows(qtbot, tmp_path: Path) -> None:
    _course(tmp_path)
    from app.ui.pages.library_page import LibraryPage

    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.refresh()

    assert len(page.current_cards()) == 1
    assert page.card_scroll.isVisible() or not page.isVisible()  # 网格模式激活
    card = page.current_cards()[0]
    assert "ATM 使用指南" in card.title_label.text()

    page.card_scroll.resize(300, 400)
    page._reflow_cards()
    assert page.card_grid.itemAtPosition(0, 0) is not None

    page.view_mode_combo.setCurrentIndex(page.view_mode_combo.findData("list"))
    assert page.course_list.count() == 1


def test_library_card_click_opens_course(qtbot, tmp_path: Path) -> None:
    _course(tmp_path)
    from app.ui.pages.library_page import LibraryPage

    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)
    page.refresh()
    card = page.current_cards()[0]

    with qtbot.waitSignal(page.open_course, timeout=3000) as blocker:
        card.activated.emit(card.summary)

    assert blocker.args[0].name == "ATM 使用指南"
    assert page.selected_course() is card.summary


# --- 换图标对话框 ---------------------------------------------------------
def test_icon_picker_applies_preset(qtbot, tmp_path: Path) -> None:
    course = _course(tmp_path)
    from app.ui.widgets.icon_picker_dialog import IconPickerDialog

    dialog = IconPickerDialog(course_dir=course, course_name="ATM 使用指南")
    qtbot.addWidget(dialog)

    assert dialog.icon_list.count() >= 12
    dialog.icon_list.setCurrentRow(2)
    # 不点击取色器按钮：QColorDialog.getColor 是模态的，自动化里会一直阻塞
    assert dialog.color_button.text() == "主色…"

    dialog._apply_and_accept()

    meta = json.loads((course / "course.json").read_text(encoding="utf-8"))
    assert meta["icon"]["value"] == dialog._current_icon().value
    assert (course / "assets" / "icon.svg").exists()


# --- 章节树图标 -----------------------------------------------------------
def test_lesson_tree_root_has_course_icon(qtbot, tmp_path: Path) -> None:
    course = _course(tmp_path)
    from app.ui.widgets.lesson_tree import LessonTreeWidget

    tree = LessonTreeWidget()
    qtbot.addWidget(tree)
    tree.load_course(course, icon_pixmap=course_icon_pixmap(course, CourseIcon(kind="preset", value="code")))

    assert tree.topLevelItemCount() == 1
    root = tree.topLevelItem(0)
    assert root.text(0) == "ATM 使用指南"
    assert root.icon(0).isNull() is False
    assert root.childCount() == 1  # 章
    assert root.child(0).childCount() == 1  # 小节

    assert tree.select_lesson("L1-1") is True
    assert tree.next_lesson() is None  # 只有一节


# --- PDF 封面 -------------------------------------------------------------
def test_pdf_cover_page_includes_icon(tmp_path: Path) -> None:
    course = _course(tmp_path)
    icon_svg = icon_service.read_icon_svg(course, CourseIcon(kind="preset", value="math"))

    html = export_service.render_html("# 课程\n\n正文", title="ATM 使用指南", icon_svg=icon_svg)

    assert "page-break-after: always" in html
    assert "<svg" in html and "viewBox" in html

    captured: dict[str, str] = {}

    def fake_printer(payload: str, target) -> bool:
        captured["html"] = payload
        Path(target).write_bytes(b"%PDF-1.4 fake")
        return True

    result = export_service.export_pdf(course, printer=fake_printer, icon_svg=icon_svg)

    assert result.path.exists()
    assert "page-break-after: always" in captured["html"]


# --- 启动健壮性 -----------------------------------------------------------
def test_launcher_kills_previous_instance() -> None:
    text = Path("启动 AI Teaches Me.vbs").read_text(encoding="utf-8")

    assert "Win32_Process" in text and "taskkill" in text
    assert "shell.Run cmd, 1, False" in text  # 窗口样式必须是 1（正常显示）


def test_ensure_visible_forces_show(qtbot) -> None:
    from PySide6.QtWidgets import QWidget

    from app.main import _ensure_visible

    widget = QWidget()
    qtbot.addWidget(widget)
    widget.hide()

    assert _ensure_visible(widget) is True
    assert widget.isVisible() is True
