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
