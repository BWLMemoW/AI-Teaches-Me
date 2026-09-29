"""M10 测试：偏好面板补齐的设置分区（FR-11 / FR-14 / FR-18 / FR-13）+ 卡片与标签条视觉回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QSizePolicy

from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.settings_service import SettingsService
from app.ui.widgets.course_card import CourseCard
from app.ui.widgets.preference_panel import PreferencePanel

pytestmark = pytest.mark.ui


def _service(tmp_path: Path) -> SettingsService:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    return SettingsService(
        result.config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())
    )


def _config(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "data" / "config.json").read_text(encoding="utf-8"))


def test_persona_section_lists_and_persists(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(panel)

    assert panel.persona_combo.count() >= 6  # 内置人设 ≥6（AC-47）
    target = panel.persona_combo.findData("strict-professor")
    panel.persona_combo.setCurrentIndex(target)

    assert service.config.persona.default == "strict-professor"
    assert _config(tmp_path)["persona"]["default"] == "strict-professor"
    assert "严格" in panel.persona_preview.toPlainText() or panel.persona_preview.toPlainText()


def test_generation_and_review_preferences_persist(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(panel)

    panel.depth_combo.setCurrentIndex(panel.depth_combo.findData("deep"))
    panel.auto_note_check.setChecked(False)
    panel.quiz_check.setChecked(False)
    panel.self_assessment_check.setChecked(False)
    panel.auto_animation_check.setChecked(False)
    panel.review_check.setChecked(False)
    panel.rounds_spin.setValue(5)
    panel.throttle_spin.setValue(55)

    saved = _config(tmp_path)["generation"]
    assert saved["default_depth"] == "deep"
    assert saved["auto_note_on_complete"] is False
    assert saved["quiz_enabled"] is False
    assert saved["self_assessment_enabled"] is False
    assert saved["auto_animation"] is False
    assert saved["svg_review"]["enabled"] is False
    assert saved["svg_review"]["max_rounds"] == 5
    assert saved["stream_throttle_ms"] == 55


def test_appearance_and_routing_preferences_persist(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(panel)

    panel.font_edit.setText("思源黑体")
    panel.font_edit.editingFinished.emit()
    panel.width_spin.setValue(960)
    panel.bg_type_combo.setCurrentIndex(panel.bg_type_combo.findData("color"))
    panel.bg_value_edit.setText("#101820")
    panel.bg_value_edit.editingFinished.emit()
    panel.bg_blur_spin.setValue(4)
    panel.bg_dim_spin.setValue(0.3)
    panel.teach_combo.setCurrentIndex(1)  # 第一个具体档案

    saved = _config(tmp_path)
    assert saved["ui"]["font_family"] == "思源黑体"
    assert saved["ui"]["reading_width"] == 960
    assert saved["ui"]["background"]["type"] == "color"
    assert saved["ui"]["background"]["value"] == "#101820"
    assert saved["ui"]["background"]["blur"] == 4
    assert saved["ui"]["background"]["dim"] == 0.3
    assert saved["routing"]["teach"] == service.config.profile_names()[0]


def test_trial_without_client_shows_hint(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses", client=None)
    qtbot.addWidget(panel)

    panel.trial_button.click()

    assert "没有可用的模型配置" in panel.trial_output.toPlainText()


def test_workspace_section_emits_backup_and_restore(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(panel)

    assert str(tmp_path) in panel.workspace_label.text()
    with qtbot.waitSignal(panel.backup_requested, timeout=2000):
        panel.backup_button.click()
    with qtbot.waitSignal(panel.restore_requested, timeout=2000):
        panel.restore_button.click()
    with qtbot.waitSignal(panel.open_folder_requested, timeout=2000) as blocker:
        panel.open_logs_button.click()
    assert blocker.args[0].endswith("logs")


def test_settings_page_includes_preference_panel(qtbot, tmp_path: Path) -> None:
    from app.ui.pages.settings_page import SettingsPage

    service = _service(tmp_path)
    page = SettingsPage(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(page)

    assert page.preference_panel is not None
    assert page.preference_panel.persona_combo.count() >= 6


# --- 视觉回归 -------------------------------------------------------------
def test_course_card_hugs_content_height(qtbot, tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    from app.services import course_service

    summary = course_service.scan_courses(tmp_path / "Courses")[0]
    card = CourseCard(summary)
    qtbot.addWidget(card)

    policy = card.sizePolicy()
    assert policy.verticalPolicy() == QSizePolicy.Policy.Maximum  # 不被网格行拉伸（避免大块空白）


def test_tag_strip_hidden_when_no_tags(qtbot, tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    from app.ui.pages.library_page import LibraryPage

    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)
    page.refresh()

    assert page.tags_list.count() == 0
    assert page.tags_list.isHidden() is True
    assert page.tag_label.isHidden() is True
    assert page.tags_list.height() <= 40  # 紧凑胶囊条，不再是一大条白底


# --- 视觉回归：文字底色 ---------------------------------------------------
def test_theme_qss_makes_labels_transparent() -> None:
    """全局 QSS 不能给 QLabel 刷背景色（否则每行文字后面会出现突兀底纹）。"""
    from app.ui.theme.presets import PRESETS
    from app.ui.theme.qss import build_qss

    for theme in PRESETS.values():
        qss = build_qss(theme, "light")
        assert "QLabel { background-color: transparent; }" in qss


def test_course_card_labels_are_transparent(qtbot, tmp_path) -> None:
    from app.bootstrap import bootstrap_workspace
    from app.infra.secret_store import InMemoryBackend, SecretStore
    from app.services import course_service
    from app.ui.widgets.course_card import CourseCard

    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    summary = course_service.scan_courses(tmp_path / "Courses")[0]
    card = CourseCard(summary)
    qtbot.addWidget(card)

    assert "#CourseCard QLabel { background-color: transparent; }" in card.styleSheet()

# --- 视觉回归：设置页布局 -------------------------------------------------
def test_settings_page_scrolls_instead_of_squashing(qtbot, tmp_path) -> None:
    """设置项多时必须滚动，而不是把分组框压扁成细条（曾经整页全是横线）。"""
    from PySide6.QtWidgets import QGroupBox, QScrollArea

    from app.ui.pages.settings_page import SettingsPage

    service = _service(tmp_path)
    page = SettingsPage(service, courses_dir=tmp_path / "Courses")
    page.resize(1000, 700)
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)

    assert page.findChild(QScrollArea) is not None
    groups = page.findChildren(QGroupBox)
    assert len(groups) >= 8  # 供应商 / 外观 / 联网 / 偏好面板 7 组 / 结果
    heights = [group.height() for group in groups]
    # 修复前：整页每个分组只剩 ~13px 的标题细条；修复后：分组保持自身高度
    assert min(heights) > 40, f"有分组被压扁成细条：{heights}"
    assert sum(1 for height in heights if height > 100) >= 6, f"多数分组仍未恢复高度：{heights}"

# --- M11：试讲修复 / 学习方式开关 / 新建人设 -----------------------------
class _FakeTrialClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.kwargs: dict = {}

    async def stream_chat(self, messages, **kwargs):
        self.messages = list(messages)
        self.kwargs = dict(kwargs)
        for piece in ("学姐说：先看两边的列数对不对，", "再动手相乘。"):
            yield piece


def test_trial_sends_persona_as_system_message(qtbot, tmp_path) -> None:
    """回归：试讲曾报 `stream_chat() got an unexpected keyword argument 'system'`。"""
    from app.ui.widgets.preference_panel import PreferencePanel

    service = _service(tmp_path)
    client = _FakeTrialClient()
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses", client=client)
    qtbot.addWidget(panel)

    panel.trial_button.click()
    qtbot.waitUntil(lambda: "学姐说" in panel.trial_output.toPlainText(), timeout=8000)

    assert "system" not in client.kwargs  # 不能再把 system 当关键字参数传给 stream_chat
    assert client.messages[0]["role"] == "system"
    assert client.messages[-1]["role"] == "user"
    assert panel.trial_output.toPlainText().strip().endswith("再动手相乘。")


def test_learning_toggles_are_explicit(qtbot, tmp_path) -> None:
    from app.ui.widgets.preference_panel import PreferencePanel

    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(panel)

    assert "不做测验" in panel.quiz_check.text()
    assert "不做作业" in panel.self_assessment_check.text()

    panel.quiz_check.setChecked(False)
    panel.self_assessment_check.setChecked(False)

    saved = _config(tmp_path)["generation"]
    assert saved["quiz_enabled"] is False
    assert saved["self_assessment_enabled"] is False


def test_persona_editor_builds_and_validates(qtbot, tmp_path) -> None:
    from app.ui.widgets.persona_editor_dialog import PersonaEditorDialog

    dialog = PersonaEditorDialog(existing_ids={"default-teacher"})
    qtbot.addWidget(dialog)
    dialog.id_edit.setText("gentle-senior")
    dialog.name_edit.setText("温柔学姐")
    dialog.avatar_edit.setText("📚")
    dialog.tone_edit.setText("温柔, 鼓励式")
    dialog.roleplay_check.setChecked(False)

    persona = dialog.build_persona()

    assert persona.id == "gentle-senior"
    assert persona.name == "温柔学姐"
    assert persona.tone == ("温柔", "鼓励式")
    assert persona.allow_roleplay is False
    assert "讲课习惯" in persona.body

    dialog.id_edit.setText("default-teacher")
    dialog._validate_live()
    assert "已存在" in dialog.error_label.text()


def test_created_persona_is_saved_and_listed(qtbot, tmp_path) -> None:
    from app.services.persona_service import PersonaService
    from app.ui.widgets.persona_editor_dialog import PersonaEditorDialog

    service = PersonaService(user_dir=tmp_path / "personas")
    dialog = PersonaEditorDialog(existing_ids={item.id for item in service.list_personas()})
    qtbot.addWidget(dialog)
    dialog.id_edit.setText("my-coach")
    dialog.name_edit.setText("我的教练")
    persona = dialog.build_persona()

    service.create_persona(persona)

    assert (tmp_path / "personas" / "my-coach.md").exists()
    assert any(item.id == "my-coach" for item in service.list_personas())

# --- M16：右键菜单与示例课程恢复 -----------------------------------------
def test_card_right_click_does_not_open_course(qtbot, tmp_path) -> None:
    """回归：右键曾触发 activated → 应用跳进课程，把上下文菜单顶掉。"""
    from PySide6.QtCore import QPoint, Qt as QtConst
    from PySide6.QtGui import QContextMenuEvent, QMouseEvent

    from app.bootstrap import bootstrap_workspace
    from app.infra.secret_store import InMemoryBackend, SecretStore
    from app.services import course_service
    from app.ui.widgets.course_card import CourseCard

    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    summary = course_service.scan_courses(tmp_path / "Courses")[0]
    card = CourseCard(summary)
    qtbot.addWidget(card)

    opened: list[object] = []
    menus: list[object] = []
    card.activated.connect(opened.append)
    card.context_requested.connect(lambda summary_, pos: menus.append((summary_, pos)))

    right = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPoint(5, 5), QtConst.MouseButton.RightButton,
        QtConst.MouseButton.RightButton, QtConst.KeyboardModifier.NoModifier,
    )
    card.mousePressEvent(right)
    assert opened == []  # 右键不能打开课程

    left = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPoint(5, 5), QtConst.MouseButton.LeftButton,
        QtConst.MouseButton.LeftButton, QtConst.KeyboardModifier.NoModifier,
    )
    card.mousePressEvent(left)
    assert opened == [summary]

    card.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(5, 5)))
    assert menus and menus[0][0] is summary


def test_restore_seed_course_rebuilds_bundle_version(tmp_path) -> None:
    from pathlib import Path

    from app.bootstrap import bootstrap_workspace, restore_seed_course

    result = bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course = result.layout.courses / "ATM 使用指南"
    target = course / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI" / "script.md"
    target.write_text("被用户改坏了", encoding="utf-8")
    (course / "outline.json").unlink()

    written = restore_seed_course(result.layout)

    assert written
    assert target.read_text(encoding="utf-8").startswith("# 先配置你的 AI")  # 恢复成内置版本
    assert (course / "outline.json").exists()
    assert len(list(course.rglob("lesson.json"))) == 5


def test_preference_panel_emits_restore_seed(qtbot, tmp_path) -> None:
    from app.ui.widgets.preference_panel import PreferencePanel

    service = _service(tmp_path)
    panel = PreferencePanel(service, courses_dir=tmp_path / "Courses")
    qtbot.addWidget(panel)

    with qtbot.waitSignal(panel.restore_seed_requested, timeout=2000):
        panel.restore_seed_button.click()

def test_sync_seed_structure_upgrades_old_course_without_touching_content(tmp_path) -> None:
    """回归：旧版示例课程（只有 1 节）在启动引导时自动补齐章节树，且不覆盖用户内容。"""
    import json

    from app.bootstrap import bootstrap_workspace, sync_seed_structure

    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    course = result.layout.courses / "ATM 使用指南"
    outline_path = course / "outline.json"
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    # 模拟「旧版」：只留 C1 的第一节，并且把 C2 目录挪走
    outline["chapters"] = [
        {
            "id": "C1",
            "title": outline["chapters"][0]["title"],
            "summary": "",
            "lessons": [outline["chapters"][0]["lessons"][0]],
        }
    ]
    outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    moved = tmp_path / "C2-moved"
    (course / "chapters" / "C2-把课程变成你自己的").rename(moved)
    script = course / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI" / "script.md"
    script.write_text("用户改过的讲义", encoding="utf-8")

    notes = sync_seed_structure(result.layout)

    upgraded = json.loads(outline_path.read_text(encoding="utf-8"))
    lesson_ids = [lesson["id"] for chapter in upgraded["chapters"] for lesson in chapter["lessons"]]
    assert lesson_ids == ["L1-1", "L1-2", "L1-3", "L2-1", "L2-2"]
    assert script.read_text(encoding="utf-8") == "用户改过的讲义"  # 用户内容一个字节都没动
    assert (course / "chapters" / "C2-把课程变成你自己的" / "chapter.json").exists()
    assert any("2 章 / 5 节" in note for note in notes)
    progress = json.loads((course / "progress.json").read_text(encoding="utf-8"))
    assert set(progress["lessons"]) >= {"L1-1", "L1-2", "L1-3", "L2-1", "L2-2"}