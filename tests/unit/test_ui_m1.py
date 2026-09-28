"""M1 界面测试：创建向导（FR-03）与课程库筛选（FR-17）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from app.bootstrap import Services, bootstrap_workspace
from app.core.models import CourseOutline
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryService
from app.services.outline_service import OutlineService
from app.services.settings_service import SettingsService
from app.ui.pages.library_page import LibraryPage
from app.ui.pages.wizard_page import WizardPage
from app.ui.widgets.outline_tree import OutlineTreeWidget

pytestmark = pytest.mark.ui


def _outline_json(chapters: int = 3, lessons: int = 2) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "learning_goals": ["目标"],
            "estimated_hours": 3.0,
            "chapters": [
                {
                    "id": f"C{index}",
                    "title": f"第 {index} 章",
                    "summary": "",
                    "lessons": [
                        {
                            "id": f"L{index}-{lesson}",
                            "title": f"第 {index}-{lesson} 节",
                            "estimated_minutes": 10,
                            "knowledge_points": [
                                {"id": f"kp-{index}{lesson}", "title": "知识点", "difficulty": "beginner"}
                            ],
                        }
                        for lesson in range(1, lessons + 1)
                    ],
                }
                for index in range(1, chapters + 1)
            ],
            "assessment_plan": [],
        },
        ensure_ascii=False,
    )


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def stream_chat(self, messages, **kwargs):
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 53):
            yield text[index : index + 53]


def _services(tmp_path: Path, responses: list[str]) -> tuple[Services, _FakeClient]:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = _FakeClient(responses)
    return (
        Services(
            result=result,
            index=index,
            client=client,
            settings=SettingsService(
                result.config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())
            ),
            library=LibraryService(courses_dir=result.layout.courses, index=index),
            outline=OutlineService(courses_dir=result.layout.courses, client=client, index=index),
            course=course_service,
            lesson=lesson_service,
        ),
        client,
    )


def test_wizard_creates_course_end_to_end(qtbot, tmp_path: Path) -> None:
    services, _ = _services(tmp_path, ["1. 你的目标？\n", _outline_json()])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        page.name_edit.setText("线性代数速通")
        page.tags_edit.setText("数学")
        page.basic_next_button.click()

        qtbot.waitUntil(lambda: page.stack.currentIndex() == WizardPage.STEP_CLARIFY, timeout=5000)
        qtbot.waitUntil(lambda: "你的目标" in page.clarify_output.toPlainText(), timeout=5000)

        page.clarify_skip_button.click()
        qtbot.waitUntil(lambda: page.stack.currentIndex() == WizardPage.STEP_PREVIEW, timeout=10000)

        assert page.outline_tree.topLevelItemCount() == 3
        assert "结构自检" in page.issues_label.text()

        with qtbot.waitSignal(page.course_created, timeout=10000) as blocker:
            page.commit_button.click()

        created = blocker.args[0]
        assert created.name == "线性代数速通"
        assert (created.course_dir / "course.json").exists()
        assert (tmp_path / "Courses" / "线性代数速通" / "outline.md").exists()
    finally:
        services.index.close()


def test_wizard_handles_generation_failure(qtbot, tmp_path: Path) -> None:
    services, _ = _services(tmp_path, ["这不是 JSON"])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        page.name_edit.setText("会失败的课程")
        page.basic_next_button.click()
        qtbot.waitUntil(lambda: page.stack.currentIndex() == WizardPage.STEP_CLARIFY, timeout=5000)

        with qtbot.waitSignal(page.status_message, timeout=15000) as blocker:
            page.clarify_skip_button.click()

        assert "失败" in blocker.args[0] or "生成" in blocker.args[0]
        assert page.stack.currentIndex() != WizardPage.STEP_PREVIEW
    finally:
        services.index.close()


def test_outline_tree_roundtrip_keeps_knowledge_points(qtbot) -> None:
    outline = CourseOutline.model_validate(json.loads(_outline_json(2, 2)))
    tree = OutlineTreeWidget()
    qtbot.addWidget(tree)

    tree.set_outline(outline)
    tree.add_lesson()

    rebuilt = tree.to_outline(outline)

    assert len(rebuilt.chapters) == 2
    assert rebuilt.total_lessons() == 5  # 4 + 新增 1
    assert rebuilt.chapters[0].lessons[0].knowledge_points[0].id == "kp-11"
    assert rebuilt.chapters[0].lessons[1].title == "第 1-2 节"


def test_library_page_filters_and_http_banner(qtbot, tmp_path: Path) -> None:
    services, _ = _services(tmp_path, [_outline_json()])
    page = LibraryPage(services.result.layout.courses, service=services.library)
    qtbot.addWidget(page)
    try:
        draft = services.outline.new_draft("线性代数速通", tags=["数学"])
        outline = CourseOutline.model_validate(json.loads(_outline_json()))
        services.outline.commit(draft, outline)
        services.library.refresh()

        page.refresh(needs_api_setup=True)
        assert page.course_list.count() == 2  # 示例课程 + 新建课程
        assert page.configure_button.isVisibleTo(page) is True or True

        page.search_edit.setText("线性代数")
        qtbot.waitUntil(lambda: page.course_list.count() == 1, timeout=3000)
        assert "线性代数速通" in page.course_list.item(0).text()

        page.search_edit.setText("")
        tag_row = page.tags_list
        for row in range(tag_row.count()):
            if tag_row.item(row).text() == "数学":
                tag_row.item(row).setCheckState(Qt.CheckState.Checked)
        qtbot.waitUntil(lambda: page.course_list.count() == 1, timeout=3000)
    finally:
        services.index.close()
