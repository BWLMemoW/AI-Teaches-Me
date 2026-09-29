"""M17 测试：课程资料文档（生成 / 落盘 / 阅读入口）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.director import DirectorAgent
from app.bootstrap import bootstrap_workspace
from app.core.models import GOAL_LABELS, CourseOutline
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.outline_service import OutlineService, build_materials_from_outline

pytestmark = pytest.mark.ui

OUTLINE = {
    "learning_goals": ["能解释矩阵乘法"],
    "chapters": [
        {
            "id": "C1",
            "title": "第一章",
            "summary": "章摘要",
            "lessons": [
                {
                    "id": "L1-1",
                    "title": "第一小节",
                    "objectives": ["目标一"],
                    "knowledge_points": [
                        {"id": "kp1", "title": "矩阵乘法", "summary": "两个变换的复合",
                         "difficulty": "intermediate", "must_master": True}
                    ],
                }
            ],
        }
    ],
}


class _FakeClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.prompts: list[str] = []

    async def stream_chat(self, messages, **kwargs):
        self.prompts.append("\n".join(str(item.get("content", "")) for item in messages))
        text = self._responses.pop(0) if self._responses else ""


def test_local_materials_cover_outline_details() -> None:
    text = build_materials_from_outline("线性代数速通", CourseOutline.model_validate(OUTLINE), GOAL_LABELS["exam"])

    assert text.startswith("# 线性代数速通 · 课程资料")
    assert "课程目标：应试" in text
    assert "### L1-1 第一小节" in text
    assert "#### 矩阵乘法" in text
    assert "两个变换的复合" in text and "进阶" in text and "必须掌握" in text
    assert "使用建议" in text


def test_commit_writes_model_materials_when_draft_has_them(tmp_path: Path) -> None:
    service = OutlineService(courses_dir=tmp_path / "Courses", client=_FakeClient())
    draft = service.new_draft("模型资料课", goal_type="deep_dive")
    draft.materials_md = "# 模型写的课程资料\n\n### L1-1 第一小节\n\n- 要点：矩阵乘法\n"

    result = service.commit(draft, CourseOutline.model_validate(OUTLINE))

    materials = result.course_dir / "materials" / "课程资料.md"
    assert "模型写的课程资料" in materials.read_text(encoding="utf-8")
    manifest = json.loads((result.course_dir / "materials" / "materials.json").read_text(encoding="utf-8"))
    assert manifest["materials"][0]["filename"] == "课程资料.md"
    assert manifest["materials"][0]["source"] == "outline"


def test_commit_falls_back_to_local_materials(tmp_path: Path) -> None:
    service = OutlineService(courses_dir=tmp_path / "Courses", client=_FakeClient())
    draft = service.new_draft("兜底资料课")  # materials_md 为空 → 本地拼装

    result = service.commit(draft, CourseOutline.model_validate(OUTLINE))

    text = (result.course_dir / "materials" / "课程资料.md").read_text(encoding="utf-8")
    assert "使用建议" in text and "#### 矩阵乘法" in text


def test_seed_sync_generates_materials_for_existing_course(tmp_path: Path) -> None:
    from app.bootstrap import sync_seed_structure

    result = bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    materials = result.layout.courses / "ATM 使用指南" / "materials" / "课程资料.md"
    if materials.exists():
        materials.unlink()

    notes = sync_seed_structure(result.layout)

    assert materials.exists()
    assert "课程资料" in materials.read_text(encoding="utf-8")
    assert any("课程资料" in note for note in notes)


def test_study_page_has_outline_and_materials_buttons(qtbot, tmp_path: Path, monkeypatch) -> None:
    from app.ui.pages.study_page import StudyPage
    from app.ui.widgets.document_viewer import DocumentViewerDialog

    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    page = StudyPage()
    qtbot.addWidget(page)
    page.open_course(tmp_path / "Courses" / "ATM 使用指南")

    assert page.outline_button.text() == "课程大纲"
    assert page.materials_button.text() == "课程资料"

    opened: list[Path] = []
    monkeypatch.setattr(DocumentViewerDialog, "exec", lambda self, *args, **kwargs: opened.append(self.path))

    page._open_document("outline")
    page._open_document("materials")

    assert any(path.name == "outline.md" for path in opened)
    assert any(path.name == "课程资料.md" for path in opened)
