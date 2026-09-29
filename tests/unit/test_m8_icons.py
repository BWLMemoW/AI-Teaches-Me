"""M8 测试：课程图标（预设 / 染色 / 自定义 / AI 生成降级）+ 课程库卡片网格。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListView

from app.bootstrap import bootstrap_workspace
from app.core.models import CourseIcon, CourseMeta, CourseOutline
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services import icon_service
from app.services.icon_service import IconService, preset_choices, preset_ids, tint_svg
from app.services.outline_service import OutlineService

GOOD_AI_SVG = (
    '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">'
    '<title>AI 图标</title><desc>课程图标</desc>'
    '<circle cx="32" cy="32" r="18" fill="none" stroke="currentColor" stroke-width="3"/></svg>'
)


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream_chat(self, messages, **kwargs):
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 61):
            yield text[index : index + 61]


def _workspace(tmp_path: Path) -> Path:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    return tmp_path / "Courses" / "ATM 使用指南"


# --- 模型与预设 -----------------------------------------------------------
def test_course_icon_validation() -> None:
    icon = CourseIcon(kind="preset", value="math", color="#e86a92", bg="#fff5f8")

    assert icon.color == "#E86A92"  # 归一化为大写
    assert icon.is_preset is True
    with pytest.raises(Exception):
        CourseIcon(color="red")


def test_presets_are_sanitizer_clean_and_tintable() -> None:
    ids = preset_ids()

    assert len(ids) >= 12
    assert {"math", "code", "ai", "general"} <= set(ids)
    for preset_id in ids:
        result = icon_service.clean(icon_service.load_preset_svg(preset_id))
        assert icon_service.is_acceptable(result), (preset_id, result.problems)

    tinted = tint_svg(icon_service.load_preset_svg("math"), "#123456")
    assert "#123456" in tinted and "currentColor" not in tinted


# --- 服务 ----------------------------------------------------------------
def test_apply_preset_writes_asset_and_returns_icon(tmp_path: Path) -> None:
    course = _workspace(tmp_path)
    result = IconService().apply_preset(course, "physics", color="#6C8AE4", bg="#EEF2FF")

    assert result.path == course / "assets" / "icon.svg"
    assert "#6C8AE4" in result.svg
    assert result.icon.kind == "preset" and result.icon.value == "physics"

    stored = IconService.store(course, result.icon)
    assert stored["icon"]["value"] == "physics"
    assert IconService.load(course).color == "#6C8AE4"


def test_load_icon_falls_back_to_default(tmp_path: Path) -> None:
    course = tmp_path / "Courses" / "坏图标课"
    course.mkdir(parents=True)
    (course / "course.json").write_text(json.dumps({"icon": {"color": "not-a-color"}}), encoding="utf-8")

    icon = IconService.load(course)

    assert icon.value == "general" and icon.kind == "preset"


def test_ai_generation_stores_svg_and_keeps_history(tmp_path: Path) -> None:
    course = _workspace(tmp_path)
    from app.agents.illustrator import IllustratorAgent

    client = _FakeClient([GOOD_AI_SVG, GOOD_AI_SVG])
    # 注入假渲染器：无界面环境里 QtSvg 无法出图，会让自检误判为失败
    service = IconService(illustrator=IllustratorAgent(client, renderer=lambda svg, width=820: b"PNG"))

    first = asyncio.run(service.generate_with_ai(course, concept="矩阵乘法", color="#E86A92"))
    second = asyncio.run(service.generate_with_ai(course, concept="矩阵乘法", color="#E86A92"))

    assert first.generated_by_ai is True
    assert first.icon.kind == "ai" and first.icon.value == "assets/icon.svg"
    assert (course / "assets" / "icon.svg").exists()
    assert (course / "assets" / "icon.v1.svg").exists()  # 历史版本
    assert second.generated_by_ai is True


def test_ai_generation_falls_back_when_svg_is_bad(tmp_path: Path) -> None:
    course = _workspace(tmp_path)
    dirty = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    from app.agents.illustrator import IllustratorAgent

    service = IconService(illustrator=IllustratorAgent(_FakeClient([dirty, dirty, dirty]), renderer=lambda svg, width=820: b"PNG"))

    result = asyncio.run(service.generate_with_ai(course, concept="x", fallback_preset="math"))

    assert result.generated_by_ai is False
    assert result.icon.kind == "preset" and result.icon.value == "math"
    assert result.issues


def test_ai_generation_without_client_uses_preset(tmp_path: Path) -> None:
    course = _workspace(tmp_path)

    result = asyncio.run(IconService(client=None).generate_with_ai(course, concept="x"))

    assert result.generated_by_ai is False
    assert any("预设" in issue for issue in result.issues)


def test_apply_svg_rejects_dangerous_input(tmp_path: Path) -> None:
    course = _workspace(tmp_path)
    dirty = '<svg xmlns="http://www.w3.org/2000/svg"><title>t</title><desc>d</desc><text x="1" y="1">没有 viewBox</text></svg>'

    result = IconService().apply_svg(course, dirty)

    assert result.icon.kind == "preset"  # 降级
    assert result.issues


# --- 建课落盘 -------------------------------------------------------------
def test_commit_writes_icon_into_course_json(tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    service = OutlineService(courses_dir=courses, client=_FakeClient([]))
    outline = CourseOutline.model_validate(
        {
            "learning_goals": ["目标"],
            "chapters": [
                {
                    "id": "C1",
                    "title": "第一章",
                    "lessons": [{"id": "L1-1", "title": "第一小节"}],
                }
            ],
        }
    )
    draft = service.new_draft("带图标的课", icon=CourseIcon(kind="preset", value="code", color="#3F7D4E"))

    result = service.commit(draft, outline)

    meta = json.loads((result.course_dir / "course.json").read_text(encoding="utf-8"))
    assert meta["icon"]["value"] == "code"
    assert meta["icon"]["color"] == "#3F7D4E"
    assert (result.course_dir / "assets" / "icon.svg").exists()
    assert CourseMeta.model_validate(meta).icon.value == "code"


def test_commit_accepts_custom_svg_icon(tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    service = OutlineService(courses_dir=courses, client=_FakeClient([]))
    outline = CourseOutline.model_validate({"chapters": [{"id": "C1", "title": "章", "lessons": []}]})
    draft = service.new_draft("自绘图标的课", icon=CourseIcon(kind="svg", value="assets/icon.svg"), icon_svg=GOOD_AI_SVG)

    result = service.commit(draft, outline)

    assert (result.course_dir / "assets" / "icon.svg").exists()
    meta = json.loads((result.course_dir / "course.json").read_text(encoding="utf-8"))
    assert meta["icon"]["kind"] == "svg"


# --- 界面 ----------------------------------------------------------------
@pytest.mark.ui
def test_library_page_card_grid_shows_icons(qtbot, tmp_path: Path) -> None:
    _workspace(tmp_path)
    from app.ui.pages.library_page import LibraryPage

    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)
    page.refresh()

    # M9 起网格模式改用真正的卡片组件（不再依赖 QListWidget 的 IconMode）
    cards = page.current_cards()
    assert len(cards) == 1
    card = cards[0]
    assert "ATM 使用指南" in card.title_label.text()
    assert card.icon_label.pixmap() is not None
    assert card.icon_label.pixmap().isNull() is False
    assert card.findChildren(object) is not None
    assert "小节" in card.meta_label.text()

    page.view_mode_combo.setCurrentIndex(page.view_mode_combo.findData("list"))
    assert page.course_list.count() == 1
    assert page.course_list.viewMode() == QListView.ViewMode.ListMode


@pytest.mark.ui
def test_course_icon_pixmap_renders(qtbot, tmp_path: Path) -> None:
    course = _workspace(tmp_path)
    from app.ui.icon_render import course_icon_pixmap

    pixmap = course_icon_pixmap(course, CourseIcon(kind="preset", value="music", color="#B4763B"), size=96)

    assert pixmap.isNull() is False
    assert pixmap.width() == 96 and pixmap.height() == 96
    assert pixmap.toImage().pixelColor(48, 48).alpha() > 0


@pytest.mark.ui
def test_wizard_icon_picker_lists_presets(qtbot, tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    courses.mkdir(parents=True)
    from app.ui.pages.wizard_page import WizardPage

    service = OutlineService(courses_dir=courses, client=_FakeClient([]))
    page = WizardPage(service)
    qtbot.addWidget(page)

    assert page.icon_list.count() == len(preset_choices())
    page.icon_list.setCurrentRow(3)
    icon = page._current_icon()
    assert icon.kind == "preset" and icon.value in preset_ids()
    assert page.icon_preview.pixmap() is not None
