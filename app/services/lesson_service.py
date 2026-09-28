"""小节内容服务（《技术文档》§4.3；M0 读取预置讲义，M2 扩展生成与落盘）。

服务层是事务边界：读讲义 + 更新 ``lesson.json`` / ``progress.json`` 都在这里完成，
UI 只拿结果。本模块**不导入 Qt**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..core.paths import atomic_write_json, read_json, read_text
from ..llm.stream import iter_ndjson

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_REVIEW_NEEDED = "review_needed"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class LessonContent:
    lesson_dir: Path
    lesson: dict
    blocks: tuple[dict, ...]
    markdown: str
    status: str = STATUS_NOT_STARTED
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def lesson_id(self) -> str:
        return str(self.lesson.get("id") or self.lesson_dir.name)

    @property
    def title(self) -> str:
        return str(self.lesson.get("title") or self.lesson_dir.name)

    @property
    def has_blocks(self) -> bool:
        return bool(self.blocks)

    @property
    def needs_markdown_fallback(self) -> bool:
        return not self.blocks and bool(self.markdown)


def blocks_to_markdown(blocks: tuple[dict, ...] | list[dict]) -> str:
    """把内容块还原成人读 Markdown（``script.md`` 缺失时的兜底）。"""
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("md", "")))
        elif kind == "callout":
            parts.append(f"> {block.get('md', '')}")
        elif kind == "analogy":
            parts.append(f"**类比 · {block.get('title', '')}**\n\n{block.get('md', '')}")
        elif kind == "table":
            headers = [str(cell) for cell in block.get("headers", [])]
            if headers:
                parts.append("| " + " | ".join(headers) + " |")
                parts.append("| " + " | ".join("---" for _ in headers) + " |")
                for row in block.get("rows", []):
                    parts.append("| " + " | ".join(str(cell) for cell in row) + " |")
        elif kind == "code":
            parts.append(f"```{block.get('lang', '')}\n{block.get('code', '')}\n```")
        elif kind == "summary":
            points = "\n".join(f"- {point}" for point in block.get("points", []))
            parts.append(f"## 小结\n\n{points}")
        elif kind == "next_preview":
            parts.append(f"## 下节预告\n\n{block.get('md', '')}")
        elif kind == "quiz":
            parts.append(f"### 小测验\n\n{block.get('question', '')}")
    return "\n\n".join(part for part in parts if part).strip() + "\n"


def load_lesson(lesson_dir: str | Path) -> LessonContent:
    """读取小节内容：优先结构化块，缺失时回退 ``script.md`` 整段渲染。"""
    directory = Path(lesson_dir)
    issues: list[str] = []

    lesson = read_json(directory / "lesson.json", default={}) or {}
    if not lesson:
        issues.append("缺少 lesson.json")

    blocks: list[dict] = []
    blocks_path = directory / "script.blocks.jsonl"
    if blocks_path.exists():
        blocks = list(iter_ndjson(read_text(blocks_path).splitlines()))
    else:
        issues.append("缺少 script.blocks.jsonl")

    markdown_path = directory / "script.md"
    markdown = read_text(markdown_path) if markdown_path.exists() else ""
    if not markdown and blocks:
        markdown = blocks_to_markdown(blocks)
        issues.append("缺少 script.md，已由内容块还原")
    if not blocks and markdown:
        issues.append("没有结构化块，按整段 Markdown 渲染")

    return LessonContent(
        lesson_dir=directory,
        lesson=lesson,
        blocks=tuple(blocks),
        markdown=markdown,
        status=str(lesson.get("status") or STATUS_NOT_STARTED),
        issues=tuple(issues),
    )


def mark_in_progress(lesson_dir: str | Path) -> dict:
    """首次打开小节：把状态推进到 ``in_progress``（已完成的保持不动）。"""
    directory = Path(lesson_dir)
    lesson = read_json(directory / "lesson.json", default={}) or {}
    if not lesson:
        return {}
    if lesson.get("status") in (STATUS_COMPLETED, STATUS_IN_PROGRESS):
        return lesson
    lesson["status"] = STATUS_IN_PROGRESS
    lesson["started_at"] = lesson.get("started_at") or now_iso()
    atomic_write_json(directory / "lesson.json", lesson)
    _sync_progress(directory)
    return lesson


def mark_complete(
    lesson_dir: str | Path,
    *,
    mastery: int | None = None,
    quiz_score: float | None = None,
    minutes: int | None = None,
) -> dict:
    """标记小节完成，并同步课程 ``progress.json``（M0 最小实现；M2 接入复习队列）。"""
    directory = Path(lesson_dir)
    lesson = read_json(directory / "lesson.json", default={}) or {}
    if not lesson:
        raise FileNotFoundError(f"缺少 lesson.json：{directory}")

    lesson["status"] = STATUS_COMPLETED
    lesson["completed_at"] = now_iso()
    if mastery is not None:
        lesson["mastery"] = mastery
    if quiz_score is not None:
        lesson["quiz_score"] = quiz_score
    if minutes is not None:
        lesson["time_spent_seconds"] = int(lesson.get("time_spent_seconds") or 0) + int(minutes) * 60
    atomic_write_json(directory / "lesson.json", lesson)
    _sync_progress(directory)
    return lesson


def unmark_complete(lesson_dir: str | Path) -> dict:
    """取消完成（内容一字不动，只改状态，见 AC-63）。"""
    directory = Path(lesson_dir)
    lesson = read_json(directory / "lesson.json", default={}) or {}
    if not lesson:
        raise FileNotFoundError(f"缺少 lesson.json：{directory}")
    lesson["status"] = STATUS_IN_PROGRESS
    lesson["completed_at"] = None
    atomic_write_json(directory / "lesson.json", lesson)
    _sync_progress(directory)
    return lesson


def course_root_of(lesson_dir: str | Path) -> Path:
    """``Courses/<课>/chapters/<章>/lessons/<节>`` → ``Courses/<课>``。"""
    return Path(lesson_dir).parents[3]


def _sync_progress(lesson_dir: Path) -> dict:
    """把小节状态汇总回课程 ``progress.json``。"""
    course_root = course_root_of(lesson_dir)
    progress_path = course_root / "progress.json"
    progress = read_json(progress_path, default={}) or {}
    lesson = read_json(lesson_dir / "lesson.json", default={}) or {}
    lesson_id = str(lesson.get("id") or lesson_dir.name.split("-")[0])

    lessons = progress.setdefault("lessons", {})
    entry = lessons.setdefault(lesson_id, {})
    entry["status"] = lesson.get("status", STATUS_NOT_STARTED)
    entry["mastery"] = lesson.get("mastery")
    entry["quiz_score"] = lesson.get("quiz_score")
    entry["minutes"] = int((lesson.get("time_spent_seconds") or 0) // 60)

    completed = [item for item in lessons.values() if item.get("status") == STATUS_COMPLETED]
    progress["total_minutes"] = sum(int(item.get("minutes") or 0) for item in lessons.values())
    progress["last_lesson"] = lesson_id
    progress["updated_at"] = now_iso()
    progress["schema_version"] = int(progress.get("schema_version") or 1)
    progress["completed_lessons"] = len(completed)
    atomic_write_json(progress_path, progress)
    return progress
