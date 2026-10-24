"""M30 测试：章节 / 小节指定参考课件（source_refs）。

三条需求的落点：
1. 每一节都可以单独指定要参考的课件 / 教材（一节一份 PPT 的粒度）；
2. 大章上的设置是小节没单独指定时的默认值（改一处带整章）；
3. 生成讲义时，指定参考是硬依据——优先于关键词自动检索，并带 ``[资料id] 文件名#定位``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.teacher import TeacherAgent
from app.bootstrap import Services, bootstrap_workspace
from app.core.paths import atomic_write_json
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryService
from app.services.material_service import (
    describe_reference_refs,
    parse_source_ref,
    resolve_reference_snippets,
)
from app.services.outline_service import OutlineService
from app.services.settings_service import SettingsService

COURSE_ID = "course-ref"


def _write_material(course_dir: Path, *, identifier: str, filename: str, kind: str, chunks: list[dict]) -> None:
    parsed_dir = course_dir / "materials" / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    entries = _manifest(course_dir)
    entries.append({"id": identifier, "filename": filename, "kind": kind, "pages": 99, "status": "parsed"})
    atomic_write_json(course_dir / "materials" / "materials.json", {"materials": entries})
    atomic_write_json(parsed_dir / f"{Path(filename).stem}.chunks.json", {"version": 1, "chunks": chunks})


def _manifest(course_dir: Path) -> list[dict]:
    path = course_dir / "materials" / "materials.json"
    if not path.exists():
        return []
    return list((json.loads(path.read_text(encoding="utf-8")) or {}).get("materials") or [])


def _make_course(root: Path, *, name: str = "参考课") -> Path:
    course_dir = root / name
    lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
    lesson_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        course_dir / "course.json",
        {"id": COURSE_ID, "name": name, "profile": {"target_audience": "自学者"}},
    )
    atomic_write_json(
        course_dir / "outline.json",
        {
            "schema_version": 1,
            "course_id": COURSE_ID,
            "chapters": [
                {
                    "id": "C1",
                    "title": "第一章",
                    "summary": "",
                    "lessons": [
                        {
                            "id": "L1-1",
                            "title": "第一小节",
                            "objectives": ["看懂指定课件"],
                            "source_refs": [],
                            "knowledge_points": [
                                {"id": "kp-1", "title": "知识点一", "summary": "摘要", "must_master": True}
                            ],
                        }
                    ],
                }
            ],
        },
    )
    atomic_write_json(lesson_dir / "lesson.json", {"id": "L1-1", "title": "第一小节", "status": "not_started"})
    _write_material(
        course_dir,
        identifier="m-deck-a",
        filename="课件A.pptx",
        kind="pptx",
        chunks=[
            {"page": 1, "heading": "封面", "text": "封面内容", "source": "text", "locator": "#s1"},
            {"page": 3, "heading": "核心", "text": "指定参考专用内容", "source": "text", "locator": "#s3"},
            {"page": 9, "heading": "附录", "text": "附录内容", "source": "text", "locator": "#s9"},
        ],
    )
    _write_material(
        course_dir,
        identifier="m-book-b",
        filename="教材B.pdf",
        kind="pdf",
        chunks=[
            {"page": 2, "heading": "", "text": "教材第二页", "source": "text", "locator": "#p2"},
            {"page": 7, "heading": "", "text": "教材第七页", "source": "text", "locator": "#p7"},
        ],
    )
    return course_dir


# --- 1. 引用写法解析 --------------------------------------------------------
def test_parse_source_ref_variants() -> None:
    assert parse_source_ref("m-deck-a") == {"material_id": "m-deck-a", "kind": "", "start": None, "end": None}
    assert parse_source_ref("m-deck-a#s3") == {"material_id": "m-deck-a", "kind": "s", "start": 3, "end": 3}
    assert parse_source_ref("m-deck-a#s3-8") == {"material_id": "m-deck-a", "kind": "s", "start": 3, "end": 8}
    assert parse_source_ref("m-book-b#p9-4") == {"material_id": "m-book-b", "kind": "p", "start": 4, "end": 9}
    assert parse_source_ref("  ") is None
    assert parse_source_ref("m-a#x3") is None


# --- 2. 指定参考的片段解析 --------------------------------------------------
def test_resolve_reference_snippets_filters_by_range(tmp_path: Path) -> None:
    course_dir = _make_course(tmp_path)

    whole = resolve_reference_snippets(course_dir, ["m-deck-a"])
    assert [item["locator"] for item in whole] == ["#s1", "#s3", "#s9"]

    ranged = resolve_reference_snippets(course_dir, ["m-deck-a#s3-4"])
    assert [item["locator"] for item in ranged] == ["#s3"]
    assert "指定参考专用内容" in ranged[0]["text"]
    assert ranged[0]["filename"] == "课件A.pptx"

    mixed = resolve_reference_snippets(course_dir, ["m-deck-a#s9", "m-book-b#p2", "m-missing"])
    assert [item["material_id"] for item in mixed] == ["m-deck-a", "m-book-b"]
    assert [item["locator"] for item in mixed] == ["#s9", "#p2"]


def test_resolve_reference_snippets_reads_manifest_only_once(tmp_path: Path) -> None:
    """空引用 / 读不懂的引用不该炸，也不该回退到「整门课乱搜」。"""
    course_dir = _make_course(tmp_path)
    assert resolve_reference_snippets(course_dir, []) == []
    assert resolve_reference_snippets(course_dir, ["", "#p1"]) == []


# --- 3. 大章默认 + 小节覆盖 -------------------------------------------------
def test_lesson_refs_override_chapter_default(tmp_path: Path) -> None:
    course_dir = _make_course(tmp_path)

    course_service.set_chapter_source_refs(course_dir, "C1", ["m-book-b"])
    inherited = course_service.reference_refs_for(course_dir, "L1-1")
    assert inherited["scope"] == "chapter"
    assert inherited["effective"] == ["m-book-b"]

    course_service.set_lesson_source_refs(course_dir, "L1-1", ["m-deck-a#s3-4"])
    own = course_service.reference_refs_for(course_dir, "L1-1")
    assert own["scope"] == "lesson"
    assert own["effective"] == ["m-deck-a#s3-4"]
    assert own["chapter"] == ["m-book-b"]  # 章上的默认还在，只是被本节盖住

    # 清空小节 = 回到继承本章
    course_service.set_lesson_source_refs(course_dir, "L1-1", [])
    assert course_service.reference_refs_for(course_dir, "L1-1")["scope"] == "chapter"

    with pytest.raises(KeyError):
        course_service.set_lesson_source_refs(course_dir, "L9-9", ["m-deck-a"])


def test_chapter_refs_survive_outline_roundtrip(tmp_path: Path) -> None:
    """章上的 source_refs 是新字段，写进 outline.json 后要能原样读回。"""
    course_dir = _make_course(tmp_path)
    course_service.set_chapter_source_refs(course_dir, "C1", ["m-deck-a", "m-book-b#p2-7"])
    saved = json.loads((course_dir / "outline.json").read_text(encoding="utf-8"))
    assert saved["chapters"][0]["source_refs"] == ["m-deck-a", "m-book-b#p2-7"]


# --- 4. 提示词：指定参考压过自动检索 ----------------------------------------
def test_teacher_prompt_marks_bound_references() -> None:
    teacher = TeacherAgent(client=object())
    prompt = teacher.build_user_prompt(
        {
            "lesson": {"id": "L1-1", "title": "第一小节", "objectives": ["看懂指定课件"]},
            "chapter": {"id": "C1", "title": "第一章"},
            "course": {"name": "参考课"},
            "knowledge_points": [{"id": "kp-1", "title": "知识点一", "summary": "摘要"}],
            "material_snippets": [
                {"material_id": "m-deck-a", "filename": "课件A.pptx", "locator": "#s3", "text": "指定参考专用内容"}
            ],
            "bound_references": [
                {"material_id": "m-deck-a", "filename": "课件A.pptx", "locator": "#s3", "text": "指定参考专用内容"}
            ],
        }
    )
    assert "本节指定参考资料" in prompt
    assert "[m-deck-a] 课件A.pptx#s3" in prompt
    assert "指定参考专用内容" in prompt
    assert "可引用的资料片段" not in prompt  # 指定参考在时不再给「随便参考」的措辞


def test_teacher_prompt_falls_back_to_auto_snippets() -> None:
    teacher = TeacherAgent(client=object())
    prompt = teacher.build_user_prompt(
        {
            "lesson": {"id": "L1-1", "title": "第一小节"},
            "chapter": {"id": "C1", "title": "第一章"},
            "course": {"name": "参考课"},
            "knowledge_points": [{"id": "kp-1", "title": "知识点一", "summary": "摘要"}],
            "material_snippets": ["m-deck-a#s1：封面内容"],
        }
    )
    assert "可引用的资料片段" in prompt
    assert "本节指定参考资料" not in prompt


def test_teacher_prompt_includes_curriculum_scope() -> None:
    teacher = TeacherAgent(client=object())
    prompt = teacher.build_user_prompt(
        {
            "lesson": {"id": "L1-2", "title": "第二小节", "objectives": ["理解当前概念"]},
            "chapter": {"id": "C1", "title": "第一章"},
            "course": {"name": "顺序课"},
            "knowledge_points": [{"id": "kp-2", "title": "当前知识点"}],
            "course_position": {
                "chapter_index": 1,
                "chapter_total": 2,
                "lesson_index": 2,
                "overall_index": 2,
                "overall_total": 5,
            },
            "covered_lessons": [
                {"id": "L1-1", "title": "第一小节", "chapter": "第一章", "knowledge_points": ["前置知识点"]}
            ],
            "covered_lesson_count": 1,
            "future_lessons": [
                {"id": "L1-3", "title": "第三小节", "knowledge_points": ["后续知识点"]}
            ],
            "future_lesson_count": 3,
            "previous_summary": "上一节讲了基础概念",
            "next_lesson": "L1-3 第三小节",
        }
    )

    assert "课程位置" in prompt and "全课第 2/5 节" in prompt
    assert "已经讲过的小节" in prompt and "前置知识点" in prompt
    assert "后面才会讲的小节" in prompt and "后续知识点" in prompt
    assert "上一小节已经讲过" in prompt
    assert "不要提前展开" in prompt
    assert "不要要求学生使用本节尚未讲到的" in prompt


# --- 5. 学习台接线：指定参考进上下文 ----------------------------------------
class _SpyRenderer:
    def __init__(self) -> None:
        from PySide6.QtWidgets import QWidget

        self._widget = QWidget()
        self.blocks: list[dict] | None = None
        self.markdown = ""

    def widget(self):
        return self._widget

    def load_page(self, html_path: Path) -> None:
        self.html_path = html_path

    def set_theme(self, css_vars: dict) -> None:
        self.theme = dict(css_vars)

    def render_blocks(self, blocks: list[dict]) -> None:
        self.blocks = list(blocks)

    def render_markdown(self, text: str) -> None:
        self.markdown = text


def _services(tmp_path: Path) -> Services:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = object()
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


@pytest.mark.ui
def test_study_page_context_uses_lesson_reference(qtbot, tmp_path: Path) -> None:
    from app.ui.pages.study_page import StudyPage

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        course_dir = _make_course(tmp_path)
        course_service.set_lesson_source_refs(course_dir, "L1-1", ["m-deck-a#s3-4"])
        page.open_course(course_dir)

        assert page._context["reference_scope"] == "lesson"
        assert page._context["reference_refs"] == ["m-deck-a#s3-4"]
        assert [item["locator"] for item in page._context["bound_references"]] == ["#s3"]
        assert page.reference_button.text().startswith("参考资料·节")
    finally:
        services.index.close()


# --- 6. 参考设置窗口的解析辅助 ----------------------------------------------
@pytest.mark.ui
def test_reference_dialog_helpers() -> None:
    from app.ui.widgets.materials_dialog import _locator_kind, _parse_range, _split_refs

    assert _parse_range("3-8") == (3, 8)
    assert _parse_range(" 5 ") == (5, None)
    assert _parse_range("3-") == (3, None)
    assert _parse_range("") == (None, None)
    assert _parse_range("abc") == (None, None)
    assert _locator_kind("pdf") == "p"
    assert _locator_kind("pptx") == "s"
    assert _locator_kind("docx") == ""
    assert _split_refs(["m-a", "m-b#p3-8", "m-a#s1"]) == {"m-a": "1", "m-b": "3-8"}


# --- 7. 补传课件的回执 ------------------------------------------------------
@pytest.mark.ui
def test_study_page_reports_material_import(qtbot, tmp_path: Path) -> None:
    from app.ui.pages.study_page import StudyPage

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        course_dir = _make_course(tmp_path)
        page.open_course(course_dir)
        assert "导入" in page.import_button.text()

        class _Result:
            materials = [
                type("M", (), {"status": "parsed", "vision_called": 2, "filename": "课件A.pptx"})(),
                type("M", (), {"status": "failed", "vision_called": 0, "filename": "坏文件.pptx"})(),
            ]

        page._report_import(_Result())
        text = page.status_label.text()
        assert "已导入 1 份课件" in text
        assert "识别资料图 2 张" in text
        assert "坏文件.pptx" in text
    finally:
        services.index.close()


# --- 8. 生成入口上的参考显性化 ----------------------------------------------
def test_describe_reference_refs_speaks_human(tmp_path: Path) -> None:
    course_dir = _make_course(tmp_path)
    text = describe_reference_refs(course_dir, ["m-deck-a#s3-4", "m-book-b"])
    assert "课件A.pptx（幻灯片 3–4）" in text
    assert "教材B.pdf" in text
    assert describe_reference_refs(course_dir, ["m-unknown"]) == "m-unknown"


class _FakeMaterialsDialog:
    """替身：记录构造参数，并按预设返回「取消 / 保存 / 本节与本章各勾了什么」。"""

    accepted = True
    lesson: list[str] = []
    chapter: list[str] = []
    skip = False
    instances: list["_FakeMaterialsDialog"] = []
    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        type(self).last_kwargs = dict(kwargs)
        type(self).instances.append(self)

    def exec(self) -> int:
        return 1 if type(self).accepted else 0

    def lesson_refs(self) -> list[str]:
        return list(type(self).lesson)

    def chapter_refs(self) -> list[str]:
        return list(type(self).chapter)

    def skip_next_time(self) -> bool:
        return bool(type(self).skip)


@pytest.mark.ui
def test_generate_button_shows_reference_state(qtbot, tmp_path: Path) -> None:
    from app.ui.pages.study_page import StudyPage

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        course_dir = _make_course(tmp_path)
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
        page.open_course(course_dir, lesson_dir=lesson_dir)
        assert page.generate_button.text() == "生成讲义"
        assert "还没有指定参考" in page.generate_button.toolTip()

        course_service.set_lesson_source_refs(course_dir, "L1-1", ["m-deck-a#s3-4"])
        page.load_lesson(lesson_dir)
        assert page.generate_button.text() == "生成讲义"
        assert "课件A.pptx" in page.generate_button.toolTip()
    finally:
        services.index.close()


@pytest.mark.ui
def test_generate_opens_prepare_dialog_and_saves_picks(qtbot, tmp_path: Path, monkeypatch) -> None:
    """点生成 → 弹准备页 → 勾选的结果写进本节参考。"""
    from app.ui.pages.study_page import StudyPage
    from app.ui.widgets import materials_dialog as materials_dialog_module

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    monkeypatch.setattr(materials_dialog_module, "MaterialsDialog", _FakeMaterialsDialog)
    _FakeMaterialsDialog.instances = []
    try:
        course_dir = _make_course(tmp_path)
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
        page.open_course(course_dir, lesson_dir=lesson_dir)
        page._load_context()

        # 取消 → 不生成，参考也不改
        _FakeMaterialsDialog.accepted = False
        assert page._open_generate_dialog() is False
        assert course_service.reference_refs_for(course_dir, "L1-1")["lesson"] == []

        # 生成并勾了资料 → 本节与本章分别写回，返回 True
        _FakeMaterialsDialog.accepted = True
        _FakeMaterialsDialog.lesson = ["m-deck-a#s3-4"]
        _FakeMaterialsDialog.chapter = ["m-book-b"]
        assert page._open_generate_dialog() is True
        info = course_service.reference_refs_for(course_dir, "L1-1")
        assert info["lesson"] == ["m-deck-a#s3-4"]
        assert info["chapter"] == ["m-book-b"]
        assert page.generate_button.text() == "生成讲义"
        assert "课件A.pptx" in page.generate_button.toolTip()

        # 准备页拿到的是课程里的资料清单（上传/选择都在它里面）
        materials = _FakeMaterialsDialog.last_kwargs["materials"]
        assert {item["id"] for item in materials} == {"m-deck-a", "m-book-b"}
        assert callable(_FakeMaterialsDialog.last_kwargs["importer"])
        assert _FakeMaterialsDialog.last_kwargs["ok_text"] == "开始生成"

        # 勾「以后不再显示」→ 写进生成偏好
        _FakeMaterialsDialog.skip = True
        assert page._open_generate_dialog() is True
        assert services.config.generation.ask_reference_when_missing is False
    finally:
        _FakeMaterialsDialog.skip = False
        services.index.close()


@pytest.mark.ui
def test_reference_dialog_saves_both_sections(qtbot, tmp_path: Path, monkeypatch) -> None:
    """工具栏的「参考资料」入口也要能一次设好本节 + 本章。"""
    from app.ui.pages.study_page import StudyPage
    from app.ui.widgets import materials_dialog as materials_dialog_module

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    monkeypatch.setattr(materials_dialog_module, "MaterialsDialog", _FakeMaterialsDialog)
    _FakeMaterialsDialog.instances = []
    _FakeMaterialsDialog.accepted = True
    _FakeMaterialsDialog.lesson = ["m-deck-a"]
    _FakeMaterialsDialog.chapter = ["m-book-b#p2-7"]
    try:
        course_dir = _make_course(tmp_path)
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
        page.open_course(course_dir, lesson_dir=lesson_dir)
        page._open_reference_dialog()

        info = course_service.reference_refs_for(course_dir, "L1-1")
        assert info["lesson"] == ["m-deck-a"]
        assert info["chapter"] == ["m-book-b#p2-7"]
        assert page.reference_button.text() == "参考资料·节 1"
        assert _FakeMaterialsDialog.last_kwargs["ok_text"] == "保存"
    finally:
        _FakeMaterialsDialog.lesson = []
        _FakeMaterialsDialog.chapter = []
        services.index.close()


@pytest.mark.ui
def test_prepare_dialog_shows_even_without_materials(qtbot, tmp_path: Path, monkeypatch) -> None:
    """回归：以前课程里没资料时什么都不弹，主人根本找不到上传入口。"""
    from app.ui.pages.study_page import StudyPage
    from app.ui.widgets import materials_dialog as materials_dialog_module

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    monkeypatch.setattr(materials_dialog_module, "MaterialsDialog", _FakeMaterialsDialog)
    _FakeMaterialsDialog.instances = []
    _FakeMaterialsDialog.accepted = False
    try:
        course_dir = tmp_path / "空资料课"
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一节"
        lesson_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(course_dir / "course.json", {"id": "c-empty", "name": "空资料课"})
        atomic_write_json(
            course_dir / "outline.json",
            {
                "schema_version": 1,
                "chapters": [
                    {"id": "C1", "title": "第一章", "lessons": [{"id": "L1-1", "title": "第一节", "knowledge_points": []}]}
                ],
            },
        )
        atomic_write_json(lesson_dir / "lesson.json", {"id": "L1-1", "title": "第一节"})

        page.open_course(course_dir, lesson_dir=lesson_dir)
        page._open_generate_dialog()
        assert len(_FakeMaterialsDialog.instances) == 1
        assert _FakeMaterialsDialog.last_kwargs["materials"] == []
    finally:
        _FakeMaterialsDialog.accepted = True
        services.index.close()


@pytest.mark.ui
def test_prepare_dialog_can_be_switched_off(qtbot, tmp_path: Path, monkeypatch) -> None:
    from app.ui.pages.study_page import StudyPage
    from app.ui.widgets import materials_dialog as materials_dialog_module

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    monkeypatch.setattr(materials_dialog_module, "MaterialsDialog", _FakeMaterialsDialog)
    _FakeMaterialsDialog.instances = []
    try:
        course_dir = _make_course(tmp_path)
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
        page.open_course(course_dir, lesson_dir=lesson_dir)
        services.config.generation.ask_reference_when_missing = False
        assert page._open_generate_dialog() is True
        assert _FakeMaterialsDialog.instances == []  # 关了窗口就不再弹
    finally:
        services.index.close()


# --- 9. 参考资料窗口本体（本节 + 本章同屏） ---------------------------------
@pytest.mark.ui
def test_clicking_chapter_opens_materials_panel_in_middle(qtbot, tmp_path: Path) -> None:
    """点左侧的章 → 中间区变成「本章资料」页；点小节再切回讲义。"""
    from PySide6.QtCore import Qt

    from app.ui.pages.study_page import StudyPage

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        course_dir = _make_course(tmp_path)
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
        page.open_course(course_dir, lesson_dir=lesson_dir)
        assert page._stack.currentIndex() == 0

        page.lesson_tree._on_item_clicked(page.lesson_tree.chapter_items()[0], 0)
        assert page._stack.currentIndex() == 1
        assert "章节资料" in page.title_label.text()
        assert page.chapter_panel.table.rowCount() == 2

        def _row_of(identifier: str) -> int:
            for row in range(page.chapter_panel.table.rowCount()):
                item = page.chapter_panel.table.item(row, 0)
                if str(item.data(Qt.ItemDataRole.UserRole)) == identifier:
                    return row
            raise AssertionError(identifier)

        page.chapter_panel.table.item(_row_of("m-book-b"), 0).setCheckState(Qt.CheckState.Checked)
        page._save_chapter_refs()
        assert course_service.reference_refs_for(course_dir, "L1-1")["chapter"] == ["m-book-b"]

        # 这一节没单独指定 → 有效参考自动变成本章默认
        page._load_context()
        assert page._context["reference_scope"] == "chapter"
        assert page.reference_button.text().startswith("参考资料·章")

        page.load_lesson(lesson_dir)
        assert page._stack.currentIndex() == 0  # 点小节回到讲义
    finally:
        services.index.close()


@pytest.mark.ui
def test_materials_dialog_two_sections_are_independent(qtbot) -> None:
    """回归：以前靠一个隐藏的「作用范围」下拉切层，切了看不出来在改哪一层。"""
    from PySide6.QtCore import Qt

    from app.ui.widgets.materials_dialog import MaterialsDialog

    dialog = MaterialsDialog(
        materials=[
            {"id": "m-a", "filename": "课件A.pptx", "kind": "pptx", "pages": 12},
            {"id": "m-b", "filename": "教材B.pdf", "kind": "pdf", "pages": 300},
        ],
        lesson_label="L1-1 第一节",
        chapter_label="C1 第一章",
    )
    qtbot.addWidget(dialog)
    assert dialog.lesson_table.rowCount() == 2
    assert dialog.chapter_table.rowCount() == 2
    assert dialog.lesson_refs() == [] and dialog.chapter_refs() == []

    def _row_of(table, identifier: str) -> int:
        for row in range(table.rowCount()):
            if str(table.item(row, 0).data(Qt.ItemDataRole.UserRole)) == identifier:
                return row
        raise AssertionError(f"表里没有 {identifier}")

    # 本节勾课件 A 的 3-8 页；本章默认勾教材 B —— 两块互不影响
    lesson_row = _row_of(dialog.lesson_table, "m-a")
    dialog.lesson_table.item(lesson_row, 0).setCheckState(Qt.CheckState.Checked)
    dialog.lesson_table.cellWidget(lesson_row, 2).setText("3-8")
    dialog.chapter_table.item(_row_of(dialog.chapter_table, "m-b"), 0).setCheckState(Qt.CheckState.Checked)
    assert dialog.lesson_refs() == ["m-a#s3-8"]
    assert dialog.chapter_refs() == ["m-b"]  # PDF 没填范围 = 整份

    # 预填：已有的设置要能原样读回
    again = MaterialsDialog(
        materials=[{"id": "m-a", "filename": "课件A.pptx", "kind": "pptx", "pages": 12}],
        lesson_refs=["m-a#s3-8"],
        chapter_refs=[],
    )
    qtbot.addWidget(again)
    assert again.lesson_refs() == ["m-a#s3-8"]
    assert again.chapter_refs() == []


@pytest.mark.ui
def test_materials_dialog_upload_auto_checks_into_lesson(qtbot, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.ui.widgets import materials_dialog as module

    class _Result:
        materials = [SimpleNamespace(material_id="m-new", status="parsed", vision_called=0)]

    shelf: list[dict] = []
    dialog = module.MaterialsDialog(materials=[], importer=lambda files: _Result(), material_loader=lambda: list(shelf))
    qtbot.addWidget(dialog)
    monkeypatch.setattr(module.QFileDialog, "getOpenFileNames", lambda *args, **kwargs: (["新课件.pptx"], ""))
    monkeypatch.setattr(
        module.QMessageBox, "question", lambda *args, **kwargs: module.QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(dialog._pool, "start", lambda task: None)  # 别真跑线程，手工喂结果

    dialog._on_upload()
    assert dialog._importing is True

    shelf.append({"id": "m-new", "filename": "新课件.pptx", "kind": "pptx", "pages": 20})
    dialog._on_import_done(_Result())
    assert dialog._importing is False
    assert dialog.lesson_refs() == ["m-new"]  # 刚上传的自动勾进本节参考
    assert dialog.chapter_refs() == []  # 本章默认不被擅自改动


@pytest.mark.ui
def test_generate_result_message_states_the_basis(qtbot, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from app.ui.pages.study_page import StudyPage

    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        course_dir = _make_course(tmp_path)
        lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一小节"
        course_service.set_lesson_source_refs(course_dir, "L1-1", ["m-deck-a#s3-4"])
        page.open_course(course_dir, lesson_dir=lesson_dir)
        page._on_done(SimpleNamespace(missing_knowledge_points=[]))
        assert "依据" in page.status_label.text()
        assert "课件A.pptx" in page.status_label.text()
        assert "未指定参考" not in page.status_label.text()

        course_service.set_lesson_source_refs(course_dir, "L1-1", [])
        page.load_lesson(lesson_dir)
        page._on_done(SimpleNamespace(missing_knowledge_points=[]))
        assert "未指定参考" in page.status_label.text()
    finally:
        services.index.close()
