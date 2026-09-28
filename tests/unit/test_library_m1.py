"""M1 课程库与课程操作测试（FR-04 / FR-17：检索、筛选、排序、标签、智能视图、重命名、删除）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_workspace
from app.core.models import CourseOutline
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryFilter, LibraryService
from app.services.outline_service import OutlineService


def _outline_json(chapters: int = 3, lessons: int = 2) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "learning_goals": ["目标"],
            "estimated_hours": 4.0,
            "chapters": [
                {
                    "id": f"C{index}",
                    "title": f"第 {index} 章 · 线性代数",
                    "summary": "摘要",
                    "lessons": [
                        {
                            "id": f"L{index}-{lesson}",
                            "title": f"第 {index}-{lesson} 节 · 矩阵",
                            "estimated_minutes": 10,
                            "knowledge_points": [
                                {"id": f"kp-{index}{lesson}", "title": "点", "difficulty": "beginner"}
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
        for index in range(0, len(text), 61):
            yield text[index : index + 61]


def _build_workspace(tmp_path: Path, *, extra_course: str = "线性代数速通"):
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    index = SqliteIndex(tmp_path / "data" / "library.db")
    service = LibraryService(courses_dir=result.layout.courses, index=index)

    outline_service = OutlineService(courses_dir=result.layout.courses, client=_FakeClient([_outline_json()]), index=index)
    draft = outline_service.new_draft(extra_course, tags=["数学"])
    outline = asyncio.run(outline_service.generate(draft))
    created = outline_service.commit(draft, outline)
    index.upsert_course(
        course_id=created.course_id,
        name=created.name,
        path=str(created.course_dir),
        total_lessons=outline.total_lessons(),
    )
    return result.layout, index, service, created


def test_refresh_merges_tags_from_index(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        summaries = service.refresh()
        by_name = {item.name: item for item in summaries}

        assert "线性代数速通" in by_name
        assert by_name["线性代数速通"].tags == ("数学",)
        assert by_name["ATM 使用指南"].seed is True
    finally:
        index.close()


def test_tags_survive_index_rebuild(tmp_path: Path) -> None:
    """删库重建后标签不丢（§3.7 可重建性规则 2）。"""
    layout, index, service, created = _build_workspace(tmp_path)
    db_path = index.path
    tag_id = service.add_tag(created.course_id, "考研", color="#E86A92")
    service.persist_state()
    index.close()

    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)

    fresh = SqliteIndex(db_path)
    try:
        assert fresh.list_tags() == []  # 删库后索引为空

        restored = LibraryService(courses_dir=layout.courses, index=fresh)
        restored.load_state()

        assert any(item["name"] == "考研" for item in fresh.list_tags())
        assert tag_id > 0
        assert (layout.courses / ".library" / "tags.json").exists()
    finally:
        fresh.close()


def test_search_covers_name_chapter_and_tags(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        assert [item.name for item in service.search(LibraryFilter(query="线性代数"))] == ["线性代数速通"]
        assert [item.name for item in service.search(LibraryFilter(query="矩阵"))] == ["线性代数速通"]
        assert [item.name for item in service.search(LibraryFilter(query="数学"))] == ["线性代数速通"]
        assert service.search(LibraryFilter(query="不存在的词")) == []
    finally:
        index.close()


def test_tag_filter_any_and_all(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        service.add_tag(created.course_id, "考研")
        service.add_tag(created.course_id, "数学")
        seed = next(item for item in course_service.scan_courses(layout.courses) if item.seed)
        service.add_tag(seed.id, "入门")

        any_result = service.search(LibraryFilter(tags=("考研", "入门"), tag_mode="any"))
        all_result = service.search(LibraryFilter(tags=("考研", "数学"), tag_mode="all"))

        assert {item.name for item in any_result} == {"线性代数速通", "ATM 使用指南"}
        assert [item.name for item in all_result] == ["线性代数速通"]
    finally:
        index.close()


def test_view_modes_and_sorting(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        course_service.mark_course_complete(created.course_dir)
        summaries = service.refresh()
        completed = service.search(LibraryFilter(view="completed"), summaries=summaries)
        in_progress = service.search(LibraryFilter(view="in_progress"), summaries=summaries)

        assert [item.name for item in completed] == ["线性代数速通"]
        assert in_progress == []

        by_name = service.search(LibraryFilter(sort="name"), summaries=summaries)
        assert [item.name for item in by_name] == ["ATM 使用指南", "线性代数速通"]

        lesson_dir = lesson_service.iter_lesson_dirs(created.course_dir)[0]
        course_service.toggle_favorite(lesson_dir, True)
        favorites = service.search(LibraryFilter(view="favorite"), summaries=service.refresh())
        assert [item.name for item in favorites] == ["线性代数速通"]

        lesson_service.set_lesson_status(lesson_dir, lesson_service.STATUS_REVIEW_NEEDED)
        progress = lesson_service.sync_course_progress(created.course_dir)
        progress["review_queue"] = [{"lesson_id": "L1-1", "mastery": 1, "excluded": False}]
        (created.course_dir / "progress.json").write_text(
            json.dumps(progress, ensure_ascii=False), encoding="utf-8"
        )
        review = service.search(LibraryFilter(view="review"), summaries=service.refresh())
        assert [item.name for item in review] == ["线性代数速通"]
    finally:
        index.close()


def test_tag_management(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        first = service.add_tag(created.course_id, "数学")
        second = service.add_tag(created.course_id, "考研")
        service.rename_tag(second, "考研冲刺")
        assert any(tag["name"] == "考研冲刺" for tag in service.tags())

        service.merge_tags(second, first)
        assert [tag["name"] for tag in service.tags()] == ["数学"]

        spare = service.add_tag(created.course_id, "临时")
        service.delete_tag(spare)
        assert all(tag["name"] != "临时" for tag in service.tags())
        assert course_service.scan_courses(layout.courses)  # 课程未被删除（AC-54）

        service.add_tag(created.course_id, "又一个临时")
        removed = service.prune_unused_tags()
        assert removed >= 0
    finally:
        index.close()


def test_saved_views_roundtrip(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        view_id = service.save_view("待复习的数学课", LibraryFilter(view="review", tags=("数学",)))

        views = service.views()
        assert views[0]["name"] == "待复习的数学课"
        assert views[0]["query"]["tags"] == ["数学"]
        assert LibraryFilter.from_dict(views[0]["query"]).view == "review"

        service.delete_view(view_id)
        assert service.views() == []
    finally:
        index.close()


def test_rename_course_updates_dir_and_meta(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        renamed = course_service.rename_course(created.course_dir, "线性代数（重命名）", index=index)

        assert renamed.name == "线性代数（重命名）"
        assert (renamed / "course.json").exists()
        meta = json.loads((renamed / "course.json").read_text(encoding="utf-8"))
        assert meta["name"] == "线性代数（重命名）"
        assert any(item["name"] == "线性代数（重命名）" for item in index.list_courses())
        assert (renamed / "chapters").is_dir()  # 内容未丢
    finally:
        index.close()


def test_mark_course_complete_and_uncomplete(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        progress = course_service.mark_course_complete(created.course_dir)
        assert progress["completed_lessons"] == 6

        progress = course_service.mark_course_complete(created.course_dir, completed=False)
        assert progress["completed_lessons"] == 0
        assert all(item["status"] == "in_progress" for item in progress["lessons"].values())
        assert (created.course_dir / "chapters").is_dir()
    finally:
        index.close()


def test_delete_course_moves_out_of_library(tmp_path: Path) -> None:
    layout, index, service, created = _build_workspace(tmp_path)
    try:
        target = created.course_dir
        removed = course_service.delete_course(target, index=index)
        assert removed is True
        assert not target.exists()
        assert all(item["name"] != "线性代数速通" for item in index.list_courses())
    finally:
        index.close()
