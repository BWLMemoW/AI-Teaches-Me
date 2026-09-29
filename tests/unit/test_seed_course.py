"""种子课程结构校验（《技术文档》§3.3 / §3.5、《项目文档》§18.6、FR-15）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.bootstrap import seed_courses_dir
from app.llm.stream import iter_ndjson

BLOCK_TYPES = {
    "meta", "text", "callout", "analogy", "diagram", "image", "svg", "animation",
    "code", "quiz", "table", "summary", "next_preview", "ref", "error", "done",
}
LESSON_STATUSES = {"not_started", "in_progress", "completed", "review_needed"}
DIFFICULTIES = {"beginner", "intermediate", "advanced"}
DEPTH_VALUES = {"minimal", "standard", "deep"}

COURSE_DIR = seed_courses_dir() / "ATM 使用指南"
LESSON_DIR = COURSE_DIR / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_course_metadata_matches_schema() -> None:
    course = _load(COURSE_DIR / "course.json")

    assert course["schema_version"] == 1
    assert course["name"] == "ATM 使用指南"
    assert course["seed"] is True
    assert course["profile"]["depth"] in DEPTH_VALUES
    assert set(course["learning"]) == {"quiz", "self_assessment", "web_search"}
    assert all(value in {"inherit", "on", "off"} for value in course["learning"].values())
    assert course["stats"]["chapters"] == 2  # M15：示例课程扩成 2 章 5 节
    assert course["stats"]["lessons"] == 5


def test_outline_has_chapter_lesson_and_knowledge_points() -> None:
    outline = _load(COURSE_DIR / "outline.json")

    assert outline["course_id"] == _load(COURSE_DIR / "course.json")["id"]
    chapter = outline["chapters"][0]
    assert chapter["id"] == "C1"
    lesson = chapter["lessons"][0]
    assert lesson["id"] == "L1-1"
    assert lesson["knowledge_points"]
    for point in lesson["knowledge_points"]:
        assert point["difficulty"] in DIFFICULTIES
        assert isinstance(point["must_master"], bool)


def test_directory_names_follow_id_title_convention() -> None:
    assert (COURSE_DIR / "chapters" / "C1-三分钟上手").is_dir()
    assert LESSON_DIR.is_dir()
    assert LESSON_DIR.name.startswith("L1-1-")


def test_chapter_and_lesson_metadata() -> None:
    chapter = _load(COURSE_DIR / "chapters" / "C1-三分钟上手" / "chapter.json")
    lesson = _load(LESSON_DIR / "lesson.json")

    assert chapter["lesson_order"] == ["L1-1", "L1-2", "L1-3"]  # M15：示例课程扩成 2 章 5 节
    assert lesson["id"] == "L1-1"
    assert lesson["status"] in LESSON_STATUSES
    assert lesson["depth"] in DEPTH_VALUES
    assert lesson["current_version"] == 1
    assert lesson["versions"][0]["version"] == 1
    assert lesson["persona"]["id"]
    assert lesson["generated_assets"] == []


def test_script_blocks_follow_protocol() -> None:
    path = LESSON_DIR / "script.blocks.jsonl"
    blocks = list(iter_ndjson(path.read_text(encoding="utf-8").splitlines()))

    assert blocks[0]["type"] == "meta"
    assert blocks[-1]["type"] == "done"
    assert blocks[0]["lesson_id"] == "L1-1"
    assert {block["type"] for block in blocks} <= BLOCK_TYPES
    assert "text" in {block["type"] for block in blocks}
    assert "summary" in {block["type"] for block in blocks}


def test_script_markdown_is_human_readable() -> None:
    text = (LESSON_DIR / "script.md").read_text(encoding="utf-8")

    assert text.startswith("# ")
    assert "小结" in text
    assert "kp-seed-001" in text


def test_progress_and_notebook_exist() -> None:
    progress = _load(COURSE_DIR / "progress.json")
    pipeline_lesson = progress["lessons"]["L1-1"]

    assert pipeline_lesson["status"] == "not_started"
    assert progress["review_queue"] == []
    assert (LESSON_DIR / "notebook.md").exists()


def test_seed_files_use_lf_line_endings() -> None:
    for name in ("course.json", "outline.json", "progress.json"):
        raw = (COURSE_DIR / name).read_bytes()
        assert b"\r\n" not in raw, name
    assert b"\r\n" not in (LESSON_DIR / "script.blocks.jsonl").read_bytes()
    assert b"\r\n" not in (LESSON_DIR / "script.md").read_bytes()
