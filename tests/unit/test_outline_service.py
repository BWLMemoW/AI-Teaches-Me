"""M1 服务层测试：大纲服务与课程目录仓库（《项目文档》§7.3、AC-08 / AC-12 / AC-15）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.director import DirectorAgent
from app.core.models import CourseOutline
from app.infra import file_repository
from app.infra.sqlite_index import SqliteIndex
from app.services.outline_service import OutlineService

def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def asyncio_run_collect(agen):
    import asyncio

    async def collect():
        return [piece async for piece in agen]

    return asyncio.run(collect())


def _outline_payload(chapters: int = 3, lessons_per_chapter: int = 2) -> str:
    payload = {
        "schema_version": 1,
        "learning_goals": ["能解释矩阵乘法"],
        "estimated_hours": 6.0,
        "chapters": [
            {
                "id": f"C{index}",
                "title": f"第 {index} 章",
                "summary": "章节摘要",
                "lessons": [
                    {
                        "id": f"L{index}-{lesson}",
                        "title": f"第 {index}-{lesson} 节",
                        "objectives": ["目标一"],
                        "estimated_minutes": 12,
                        "knowledge_points": [
                            {
                                "id": f"kp-{index}{lesson}",
                                "title": "知识点",
                                "summary": "一句话解释",
                                "difficulty": "beginner",
                                "must_master": True,
                            }
                        ],
                    }
                    for lesson in range(1, lessons_per_chapter + 1)
                ],
            }
            for index in range(1, chapters + 1)
        ],
        "assessment_plan": ["每章末自测"],
    }
    return json.dumps(payload, ensure_ascii=False)


class _FakeClient:
    """按顺序吐出预设文本的桩客户端（模拟流式分片）。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream_chat(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 37):
            yield text[index : index + 37]


def _service(tmp_path: Path, responses: list[str]) -> tuple[OutlineService, _FakeClient, SqliteIndex]:
    courses = tmp_path / "Courses"
    courses.mkdir(parents=True, exist_ok=True)
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = _FakeClient(responses)
    return OutlineService(courses_dir=courses, client=client, index=index), client, index


def test_new_draft_dedupes_existing_course_name(tmp_path: Path) -> None:
    service, _, index = _service(tmp_path, [])
    try:
        first = service.new_draft("线性代数速通")
        service.commit(first, CourseOutline.model_validate(json.loads(_outline_payload(1, 1))))

        second = service.new_draft("线性代数速通")

        assert first.name == "线性代数速通"
        assert second.name == "线性代数速通-2"
    finally:
        index.close()


def test_generate_and_commit_writes_full_course_structure(tmp_path: Path) -> None:
    service, client, index = _service(tmp_path, [_outline_payload(3, 2)])
    try:
        draft = service.new_draft("线性代数速通", user_outline="按矩阵→行列式→特征值", tags=["数学"])
        outline = asyncio_run(service.generate(draft))

        assert len(outline.chapters) == 3
        assert outline.total_lessons() == 6
        assert len(client.calls) == 1  # 结构达标，不需要细化轮次
        assert service.validate(outline) == []

        result = service.commit(draft, outline)

        course_dir = result.course_dir
        assert (course_dir / "course.json").exists()
        assert (course_dir / "outline.json").exists()
        assert (course_dir / "outline.md").exists()
        assert (course_dir / "progress.json").exists()
        lesson_dir = course_dir / "chapters" / "C1-第 1 章" / "lessons" / "L1-1-第 1-1 节"
        assert (lesson_dir / "lesson.json").exists()
        assert (lesson_dir / "notebook.md").exists()

        course_meta = json.loads((course_dir / "course.json").read_text(encoding="utf-8"))
        assert course_meta["name"] == "线性代数速通"
        assert course_meta["stats"]["lessons"] == 6
        progress = json.loads((course_dir / "progress.json").read_text(encoding="utf-8"))
        assert progress["lessons"]["L1-1"]["status"] == "not_started"

        assert [item["name"] for item in index.list_tags()] == ["数学"]
        assert index.list_courses()[0]["name"] == "线性代数速通"
    finally:
        index.close()


def test_thin_outline_triggers_refinement_round(tmp_path: Path) -> None:
    service, client, index = _service(tmp_path, [_outline_payload(1, 1), _outline_payload(3, 2)])
    try:
        draft = service.new_draft("只需要一章")
        outline = asyncio_run(service.generate(draft))

        assert len(client.calls) == 2
        assert len(outline.chapters) == 3
    finally:
        index.close()


def test_validate_reports_structure_problems() -> None:
    thin = CourseOutline.model_validate(json.loads(_outline_payload(1, 1)))
    issues = OutlineService.validate(thin)

    assert any("章节偏少" in issue for issue in issues)
    assert any("小节偏少" in issue for issue in issues)
    assert OutlineService.validate(CourseOutline()) == ["大纲里没有任何章节"]


def test_refinement_failure_keeps_first_outline(tmp_path: Path) -> None:
    service, client, index = _service(tmp_path, [_outline_payload(1, 1), "这不是 JSON"])
    try:
        draft = service.new_draft("细化失败也要能用")
        outline = asyncio_run(service.generate(draft))

        assert len(outline.chapters) == 1  # 保留原草稿，不阻塞用户
    finally:
        index.close()


def test_clarify_streams_and_records_questions(tmp_path: Path) -> None:
    service, _, index = _service(tmp_path, ["1. 你的目标？\n2. 你的基础？\n"])
    try:
        draft = service.new_draft("待澄清课程")

        parts = asyncio_run_collect(service.ask_clarify(draft))

        assert "你的目标" in "".join(parts)
        assert "你的基础" in draft.clarify_questions
    finally:
        index.close()


def test_delete_to_recycle_bin_removes_directory(tmp_path: Path) -> None:
    target = tmp_path / "Courses" / "要删的课程"
    target.mkdir(parents=True)
    (target / "course.json").write_text("{}", encoding="utf-8")

    assert file_repository.delete_to_recycle_bin(target) is True
    assert not target.exists()
    assert file_repository.delete_to_recycle_bin(target) is False


def test_unique_course_dir_appends_suffix(tmp_path: Path) -> None:
    courses = tmp_path / "Courses"
    (courses / "线性代数").mkdir(parents=True)
    (courses / "线性代数-2").mkdir(parents=True)

    name, path = file_repository.unique_course_dir(courses, "线性代数")

    assert name == "线性代数-3"
    assert path.name == "线性代数-3"


def test_ensure_course_dirs_matches_doc_layout(tmp_path: Path) -> None:
    created = file_repository.ensure_course_dirs(tmp_path / "课程")

    for key in ("assets", "materials/raw", "materials/parsed", "chapters", "notes", "qa"):
        assert created[key].is_dir()


def test_prompt_files_exist_for_director() -> None:
    agent = DirectorAgent(_FakeClient([]))
    prompt = agent.system_prompt(phase="generate")
    clarify = agent.system_prompt(phase="clarify")

    assert "教学大纲" in prompt and "严格 JSON" in prompt
    assert "澄清问题" in clarify
