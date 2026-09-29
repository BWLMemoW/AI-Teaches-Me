"""M13 测试：创建课程时的「课程目标」（通用 / 应试 / 速成 / 兴趣 / 精讲）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.director import GOAL_HINTS, DirectorAgent
from app.bootstrap import bootstrap_workspace
from app.core.models import GOAL_LABELS, GOAL_TYPES, CourseOutline
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.outline_service import OutlineService
from app.ui.pages.wizard_page import WizardPage

pytestmark = pytest.mark.ui


class _FakeClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.prompts: list[str] = []

    async def stream_chat(self, messages, **kwargs):
        self.prompts.append("\n".join(str(item.get("content", "")) for item in messages))
        text = self._responses.pop(0) if self._responses else ""


def test_goal_types_cover_requested_options() -> None:
    assert GOAL_TYPES == ("general", "exam", "crash", "interest", "deep_dive")
    assert GOAL_LABELS["exam"] == "应试"
    assert GOAL_LABELS["crash"] == "速成"
    assert GOAL_LABELS["interest"] == "兴趣"
    assert GOAL_LABELS["deep_dive"] == "精讲"
    assert set(GOAL_HINTS) == set(GOAL_TYPES)
    assert "考点" in GOAL_HINTS["exam"]
    assert "3-5 小时" in GOAL_HINTS["crash"]


def test_wizard_goal_selector_defaults_and_switches(qtbot, tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    courses.mkdir(parents=True)
    page = WizardPage(OutlineService(courses_dir=courses, client=_FakeClient()))
    qtbot.addWidget(page)

    assert set(page.goal_buttons) == set(GOAL_TYPES)
    assert page._current_goal() == "general"  # 默认「通用」
    assert page.goal_buttons["general"].isChecked() is True

    page.goal_buttons["exam"].setChecked(True)
    assert page._current_goal() == "exam"


def test_director_prompt_carries_goal_orientation(qtbot) -> None:
    agent = DirectorAgent(_FakeClient())

    prompt = agent.build_user_prompt(course_name="线性代数速通", goal_type="exam")

    assert "课程目标" in prompt
    assert "应试" in prompt and "考点" in prompt
    assert "速成" not in prompt  # 只带当前选中的取向


def test_commit_stores_goal_type_in_course_json(tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    service = OutlineService(courses_dir=courses, client=_FakeClient())
    outline = CourseOutline.model_validate({"chapters": [{"id": "C1", "title": "章", "lessons": []}]})
    draft = service.new_draft("速成版线性代数", goal_type="crash")

    assert draft.goal_type == "crash"
    result = service.commit(draft, outline)

    meta = json.loads((result.course_dir / "course.json").read_text(encoding="utf-8"))
    assert meta["profile"]["goal_type"] == "crash"


def test_wizard_passes_goal_into_draft(qtbot, tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    courses.mkdir(parents=True)
    page = WizardPage(OutlineService(courses_dir=courses, client=_FakeClient()))
    qtbot.addWidget(page)
    page.name_edit.setText("兴趣向课程")
    page.goal_buttons["interest"].setChecked(True)

    page._on_basic_next()

    assert page._draft is not None
    assert page._draft.goal_type == "interest"


def test_wizard_ai_icon_uses_workspace_cache_dir() -> None:
    """回归：向导草稿阶段的 AI 图标必须写进工作区缓存目录，绝不能写到当前工作目录。

    （曾发生：使用 Path(".") 导致仓库根出现 assets/icon.svg。）
    """
    source = (Path("app/ui/pages/wizard_page.py")).read_text(encoding="utf-8")

    assert "draft_icon" in source
    assert 'generate_with_ai(' in source
    compact = source.replace(" ", "").replace("\n", "")
    assert 'generate_with_ai(Path(".")' not in compact
