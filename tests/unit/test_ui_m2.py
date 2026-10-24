"""M2 界面测试：学习台（章节树 / 生成讲义 / 追问 / 深度与人设 / 自评与复习）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from app.bootstrap import Services, bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryService
from app.services.outline_service import OutlineService
from app.services.settings_service import SettingsService
from app.ui.pages.study_page import StudyPage

pytestmark = pytest.mark.ui


class _StubRenderer:
    def __init__(self) -> None:
        self._widget = QWidget()
        self.blocks: list[dict] | None = None
        self.markdown: str | None = None

    def widget(self) -> QWidget:
        return self._widget

    def load_page(self, html_path: Path) -> None:
        self.html_path = html_path

    def set_theme(self, css_vars: dict) -> None:
        self.theme = dict(css_vars)

    def render_blocks(self, blocks: list[dict]) -> None:
        self.blocks = list(blocks)

    def render_markdown(self, text: str) -> None:
        self.markdown = text


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream_chat(self, messages, **kwargs):
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 41):
            yield text[index : index + 41]


def _ndjson(blocks: list[dict]) -> str:
    return "\n".join(json.dumps(block, ensure_ascii=False) for block in blocks) + "\n"


def _services(tmp_path: Path, responses: list[str]) -> Services:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    config = result.config
    config.generation.quiz_enabled = True
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = _FakeClient(responses)
    return Services(
        result=result,
        index=index,
        client=client,
        settings=SettingsService(config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())),
        library=LibraryService(courses_dir=result.layout.courses, index=index),
        outline=OutlineService(courses_dir=result.layout.courses, client=client, index=index),
        course=course_service,
        lesson=lesson_service,
    )


def _seed_course(tmp_path: Path) -> Path:
    return tmp_path / "Courses" / "ATM 使用指南"


def _seed_lesson(tmp_path: Path) -> Path:
    return _seed_course(tmp_path) / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


def test_study_page_opens_course_and_builds_tree(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, [])
    renderer = _StubRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))

        assert page.lesson_tree.topLevelItemCount() == 1
        assert page.lesson_tree.topLevelItem(0).childCount() == 2  # 两个章
        assert renderer.blocks and renderer.blocks[0]["type"] == "meta"
        assert "L1-1" in page.title_label.text()
        assert page.persona_combo.count() >= 6  # 内置人设 ≥6（AC-47）
    finally:
        services.index.close()


def test_lesson_settings_follow_global_defaults_across_switches(qtbot, tmp_path: Path) -> None:
    from app.services.persona_service import PersonaService

    services = _services(tmp_path, [])
    config = services.config
    personas = PersonaService(user_dir=services.result.layout.personas)
    config.persona.default = personas.list_personas()[-1].id
    config.generation.default_depth = "deep"
    lessons = list((_seed_course(tmp_path) / "chapters").glob("*/lessons/*/lesson.json"))
    for path in lessons:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["persona"] = {"id": "inherit", "version": 1}
        data["depth"] = "inherit"
        data["settings_version"] = lesson_service.SETTINGS_VERSION
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))
        first = page._lesson_dir
        assert page.depth_combo.currentData() == "inherit"
        assert page._effective_depth() == "deep"
        assert page.persona_combo.currentData() == "inherit"
        assert page._current_persona().id == config.persona.default

        second = next(path.parent for path in lessons if path.parent != first)
        page.load_lesson(second)

        assert page.depth_combo.currentData() == "inherit"
        assert page._effective_depth() == "deep"
        assert page.persona_combo.currentData() == "inherit"
    finally:
        services.index.close()


def test_lesson_context_carries_previous_and_future_scope(qtbot, tmp_path: Path) -> None:
    """回归：生成讲义时要知道全课顺序，而不是只看到当前一小节。"""
    services = _services(tmp_path, [])
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        tree = course_service.lesson_tree(_seed_course(tmp_path))
        lessons = [item for chapter in tree for item in chapter["lessons"]]
        target = lessons[1]
        page.open_course(_seed_course(tmp_path), lesson_dir=target["path"])

        context = page._context
        assert context["course_position"]["overall_index"] == 2
        assert context["course_position"]["overall_total"] == len(lessons)
        assert context["covered_lesson_count"] == 1
        assert context["covered_lessons"][-1]["id"] == lessons[0]["id"]
        assert context["future_lesson_count"] == len(lessons) - 2
        assert context["future_lessons"][0]["id"] == lessons[2]["id"]
        assert context["next_lesson"].startswith(lessons[2]["id"])
        assert context["previous_summary"]
        assert context["outline"]["chapters"]
    finally:
        services.index.close()


def test_apply_settings_to_course_writes_every_lesson(qtbot, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    services = _services(tmp_path, [])
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))
        page.depth_combo.setCurrentIndex(page.depth_combo.findData("deep"))
        page.quiz_toggle.setCurrentIndex(page.quiz_toggle.findData("off"))
        page.self_toggle.setCurrentIndex(page.self_toggle.findData("on"))
        persona_id = str(page.persona_combo.itemData(1))
        page.persona_combo.setCurrentIndex(1)
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )

        page._apply_settings_to_course()

        paths = list((_seed_course(tmp_path) / "chapters").glob("*/lessons/*/lesson.json"))
        assert len(paths) >= 2
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["depth"] == "deep"
            assert data["persona"]["id"] == persona_id
            assert data["assessment"] == {"quiz": "off", "self_assessment": "on"}
    finally:
        services.index.close()


def test_legacy_lesson_defaults_migrate_once(tmp_path: Path) -> None:
    services = _services(tmp_path, [])
    try:
        lesson_dirs = sorted((_seed_course(tmp_path) / "chapters").glob("*/lessons/*"))
        first = lesson_dirs[0] / "lesson.json"
        second = lesson_dirs[1] / "lesson.json"
        data = json.loads(first.read_text(encoding="utf-8"))
        data.update({"depth": "standard", "persona": {"id": "default-teacher", "version": 1}})
        data.pop("settings_version", None)
        first.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        data = json.loads(second.read_text(encoding="utf-8"))
        data.update({"depth": "deep", "persona": {"id": "catgirl-assistant", "version": 1}})
        data.pop("settings_version", None)
        second.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        lesson_service.migrate_legacy_lesson_settings(_seed_course(tmp_path))

        migrated = json.loads(first.read_text(encoding="utf-8"))
        kept = json.loads(second.read_text(encoding="utf-8"))
        assert migrated["depth"] == "inherit"
        assert migrated["persona"]["id"] == "inherit"
        assert migrated["settings_version"] == lesson_service.SETTINGS_VERSION
        assert kept["depth"] == "deep"
        assert kept["persona"]["id"] == "catgirl-assistant"
        assert lesson_service.migrate_legacy_lesson_settings(_seed_course(tmp_path)) == 0
    finally:
        services.index.close()


def test_study_page_generates_script_and_bumps_version(qtbot, tmp_path: Path, monkeypatch) -> None:
    blocks = [
        {"type": "meta", "lesson_id": "L1-1", "depth": "standard", "version": 1},
        {"type": "text", "md": "## 工作区与课程数据分离\n\nCourses/ 是你的课程资产。"},
        {"type": "text", "md": "## 供应商档案与密钥存储\n\n密钥进系统凭据库，不落明文。"},
        {"type": "text", "md": "## 测试连接看什么\n\n握手耗时与模型列表。"},
        {"type": "done", "tokens": 0},
    ]
    services = _services(tmp_path, [_ndjson(blocks)])
    renderer = _StubRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))
        # 生成前会弹「准备页」（上传 / 选资料 / 直接生成）；这里只验证生成与版本号
        monkeypatch.setattr(page, "_open_generate_dialog", lambda: True)
        page.generate_button.click()

        qtbot.waitUntil(lambda: "讲义已生成" in page.status_label.text(), timeout=20000)

        lesson = json.loads((_seed_lesson(tmp_path) / "lesson.json").read_text(encoding="utf-8"))
        assert lesson["current_version"] == 2
        assert lesson["status"] == "in_progress"
        assert renderer.blocks is not None and len(renderer.blocks) >= 4
        assert "密钥存储" in page.status_label.text() or "覆盖" in page.status_label.text()
    finally:
        services.index.close()


def test_study_page_ask_question_writes_chat_jsonl(qtbot, tmp_path: Path) -> None:
    answer = _ndjson(
        [
            {"type": "meta", "lesson_id": "L1-1"},
            {"type": "text", "md": "因为两边变换要能接上。"},
            {"type": "done", "tokens": 0},
        ]
    )
    services = _services(tmp_path, [answer])
    renderer = _StubRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))
        page.chat_drawer.input_edit.setPlainText("为什么行数等于列数才能相乘？")
        page.chat_drawer.send_button.click()

        qtbot.waitUntil(lambda: "因为两边变换要能接上" in page.chat_drawer.history.toPlainText(), timeout=20000)
        # 界面先渲染、chat.jsonl 后落盘：等记录写完再断言（否则是测试自身竞态）
        qtbot.waitUntil(
            lambda: len(lesson_service.load_chat(_seed_lesson(tmp_path))) >= 2, timeout=20000
        )

        records = lesson_service.load_chat(_seed_lesson(tmp_path))
        assert [item["role"] for item in records] == ["user", "assistant"]
        assert records[1]["blocks"] >= 1
    finally:
        services.index.close()


def test_study_page_persists_depth_persona_and_assessment(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, [])
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))

        page.depth_combo.setCurrentIndex(page.depth_combo.findData("deep"))
        page.quiz_toggle.setCurrentIndex(page.quiz_toggle.findData("off"))
        page.self_toggle.setCurrentIndex(page.self_toggle.findData("on"))
        page.persona_combo.setCurrentIndex(page.persona_combo.findData("strict-professor"))

        lesson = json.loads((_seed_lesson(tmp_path) / "lesson.json").read_text(encoding="utf-8"))
        assert lesson["depth"] == "deep"
        assert lesson["assessment"] == {"quiz": "off", "self_assessment": "on"}
        assert lesson["persona"]["id"] == "strict-professor"
    finally:
        services.index.close()


def test_study_page_mark_complete_updates_progress_and_review(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, [])
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))
        page.mastery_combo.setCurrentIndex(page.mastery_combo.findData(1))  # 没懂 → 进复习队列

        page.complete_button.click()

        progress = json.loads((_seed_course(tmp_path) / "progress.json").read_text(encoding="utf-8"))
        assert progress["lessons"]["L1-1"]["status"] == "completed"
        assert progress["lessons"]["L1-1"]["mastery"] == 1
        assert progress["review_queue"][0]["lesson_id"] == "L1-1"
    finally:
        services.index.close()


def test_study_page_generation_failure_is_reported(qtbot, tmp_path: Path, monkeypatch) -> None:
    services = _services(tmp_path, ["这不是 NDJSON"])
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(_seed_course(tmp_path))
        monkeypatch.setattr(page, "_open_generate_dialog", lambda: True)
        page.generate_button.click()

        qtbot.waitUntil(lambda: "失败" in page.status_label.text() or "覆盖" in page.status_label.text(), timeout=20000)

        assert page.generate_button.isEnabled() is True
    finally:
        services.index.close()
