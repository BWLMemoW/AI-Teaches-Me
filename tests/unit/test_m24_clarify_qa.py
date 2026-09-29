"""M24 测试：创建向导第二步改成「一问一答」列表（可滚动）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QScrollArea

from app.bootstrap import Services, bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryService
from app.services.outline_service import OutlineService
from app.services.settings_service import SettingsService
from app.ui.pages.wizard_page import WizardPage, parse_clarify_questions

pytestmark = pytest.mark.ui

SAMPLE = (
    "在生成大纲前，先确认 3 件事（回答后我直接出大纲；若想跳过，回复「跳过，直接生成」）：\n"
    "\n"
    "1. 学这门课主要为了什么——通过考试、还是动手做项目计算？\n"
    "2. 你目前的线性代数基础在哪一档——零基础、还是学过但忘了？\n"
    "3. 每周能稳定投入多少小时——这决定我拆成 3 章还是 8 章？\n"
    "\n"
    "补充说明：本课程暂无用户大纲，若你跳过提问，我将按【零基础 → 标准深度】搭建。\n"
)


def _outline_json(chapters: int = 2, lessons: int = 2) -> str:
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

    async def stream_chat(self, messages, **kwargs):
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 47):
            yield text[index : index + 47]


def _services(tmp_path: Path, responses: list[str]) -> Services:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = _FakeClient(responses)
    return Services(
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
    )


# --- 解析器 ---------------------------------------------------------------
def test_parse_splits_intro_questions_and_trailer() -> None:
    intro, questions, trailer = parse_clarify_questions(SAMPLE)
    assert intro.startswith("在生成大纲前，先确认 3 件事")
    assert len(questions) == 3
    assert questions[0].startswith("学这门课主要为了什么")
    assert questions[2].startswith("每周能稳定投入多少小时")
    assert trailer.startswith("补充说明：")


def test_parse_merges_continuation_lines() -> None:
    _intro, questions, _trailer = parse_clarify_questions("1. 第一问\n   换行继续\n2. 第二问\n")
    assert questions == ["第一问 换行继续", "第二问"]


def test_parse_supports_bullets() -> None:
    _intro, questions, _trailer = parse_clarify_questions("- 甲\n- 乙\n* 丙\n")
    assert questions == ["甲", "乙", "丙"]


def test_parse_empty_text() -> None:
    assert parse_clarify_questions("") == ("", [], "")


# --- 界面 -----------------------------------------------------------------
def test_clarify_step_renders_one_input_per_question(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, [SAMPLE, _outline_json()])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        page.name_edit.setText("线性代数速通")
        page.basic_next_button.click()
        qtbot.waitUntil(lambda: page.stack.currentIndex() == WizardPage.STEP_CLARIFY, timeout=5000)
        qtbot.waitUntil(lambda: len(page.clarify_answer_edits) == 3, timeout=10000)

        assert len(page.clarify_question_labels) == 3
        assert "问题 1：" in page.clarify_question_labels[0].text()
        assert "每周能稳定投入多少小时" in page.clarify_question_labels[2].text()
        # 原始文本仍保留在隐藏缓冲区里（兼容既有行为）
        assert "学这门课主要为了什么" in page.clarify_output.toPlainText()
        # 列表放在可滚动区域里
        assert isinstance(page.clarify_scroll, QScrollArea)
        assert page.clarify_qa_layout.parent() is page.clarify_scroll.widget()
        assert "补充说明" in page.clarify_trailer_label.text()

        page.clarify_answer_edits[0].setPlainText("为了动手做项目")
        page.clarify_answer_edits[2].setPlainText("每周 4 小时")
        page.clarify_skip_button.click()
        qtbot.waitUntil(lambda: page.stack.currentIndex() == WizardPage.STEP_PREVIEW, timeout=15000)

        answers = page._draft.clarify_answers
        assert "为了动手做项目" in answers and "每周 4 小时" in answers
        assert "问题 2" not in answers  # 没回答的那条不塞进上下文
    finally:
        services.index.close()


def test_answers_survive_a_streaming_rebuild(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, [SAMPLE, _outline_json()])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        page.name_edit.setText("重绘测试")
        page.basic_next_button.click()
        qtbot.waitUntil(lambda: len(page.clarify_answer_edits) >= 1, timeout=10000)
        page.clarify_answer_edits[0].setPlainText("已经写了一半")
        page.clarify_output.appendPlainText("\n4. 追加的第四问？\n")
        page._render_clarify_questions()
        assert len(page.clarify_answer_edits) == 4
        assert page.clarify_answer_edits[0].toPlainText() == "已经写了一半"
    finally:
        services.index.close()


def test_free_form_fallback_keeps_single_box(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, ["随便聊聊这门课吧，你想学点什么？\n", _outline_json()])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        page.name_edit.setText("自由回答")
        page.basic_next_button.click()
        qtbot.waitUntil(lambda: len(page.clarify_answer_edits) == 1, timeout=10000)
        assert "随便聊聊" in page.clarify_intro_label.text()
        page.clarify_answer_edits[0].setPlainText("我想先补基础")
        assert page._clarify_answers_markdown() == "我想先补基础"
    finally:
        services.index.close()

def test_answers_button_label(qtbot, tmp_path: Path) -> None:
    """主人指定的按钮文案。"""
    services = _services(tmp_path, [SAMPLE, _outline_json()])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        assert page.clarify_skip_button.text() == "我已答完，生成大纲"
    finally:
        services.index.close()


def test_generation_failure_stays_on_third_step(qtbot, tmp_path: Path) -> None:
    """回归：生成失败时曾经一闪而过地退回第二步，看起来像「点了没用」。"""
    services = _services(tmp_path, [SAMPLE, "这不是 JSON"])
    page = WizardPage(services.outline)
    qtbot.addWidget(page)
    try:
        page.name_edit.setText("失败也要看得见")
        page.basic_next_button.click()
        qtbot.waitUntil(lambda: len(page.clarify_answer_edits) >= 1, timeout=10000)
        page.clarify_answer_edits[0].setPlainText("零基础")
        page.clarify_skip_button.click()
        qtbot.waitUntil(lambda: page.generate_error_label.text() != "", timeout=20000)

        assert page.stack.currentIndex() == WizardPage.STEP_GENERATE  # 停在第三步，不再弹回
        assert "生成失败" in page.generate_error_label.text()
        assert page.generate_error_label.isVisibleTo(page)
        assert page.generate_retry_button.text() == "重试"

        page.generate_back_button.click()
        assert page.stack.currentIndex() == WizardPage.STEP_CLARIFY
    finally:
        services.index.close()