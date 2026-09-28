"""课程库服务（《技术文档》§2.7；M0 只做扫描与读取，M1 扩展创建与编辑）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import read_json


@dataclass(frozen=True)
class CourseSummary:
    """课程库里的一张卡片（对应 §3.5.1 的 course.json）。"""

    id: str
    name: str
    path: Path
    description: str = ""
    tags: tuple[str, ...] = ()
    theme: str | None = None
    persona: str | None = None
    seed: bool = False
    pinned: bool = False
    sort_index: int = 0
    chapters: int = 0
    lessons: int = 0
    completed_lessons: int = 0
    broken: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def progress_ratio(self) -> float:
        if self.lessons <= 0:
            return 0.0
        return min(1.0, self.completed_lessons / self.lessons)


def load_course(course_dir: str | Path) -> tuple[dict, list[str]]:
    """读取 ``course.json``，返回（数据, 问题列表）。缺失关键文件即视为「需要修复」。"""
    directory = Path(course_dir)
    issues: list[str] = []
    meta_path = directory / "course.json"
    if not meta_path.exists():
        return {}, [f"缺少 course.json：{directory.name}"]
    try:
        data = read_json(meta_path, default={}) or {}
    except ValueError as exc:
        return {}, [f"course.json 无法解析：{exc}"]
    if not isinstance(data, dict):
        return {}, ["course.json 内容不是对象"]
    if not data.get("name"):
        issues.append("course.json 缺少 name")
    if not (directory / "outline.json").exists():
        issues.append("缺少 outline.json")
    return data, issues


def lesson_tree(course_dir: str | Path) -> list[dict]:
    """章 → 小节的树（读 outline.json，并合并 lesson.json 的状态）。"""
    directory = Path(course_dir)
    outline = read_json(directory / "outline.json", default={}) or {}
    tree: list[dict] = []
    for chapter in outline.get("chapters", []) or []:
        chapter_id = str(chapter.get("id", ""))
        lessons: list[dict] = []
        for lesson in chapter.get("lessons", []) or []:
            lesson_id = str(lesson.get("id", ""))
            lesson_dir = find_lesson_dir(directory, lesson_id)
            state = read_json(lesson_dir / "lesson.json", default={}) if lesson_dir else {}
            lessons.append(
                {
                    "id": lesson_id,
                    "title": lesson.get("title", ""),
                    "estimated_minutes": lesson.get("estimated_minutes"),
                    "knowledge_points": lesson.get("knowledge_points", []) or [],
                    "status": (state or {}).get("status", "not_started"),
                    "mastery": (state or {}).get("mastery"),
                    "path": lesson_dir,
                }
            )
        tree.append(
            {
                "id": chapter_id,
                "title": chapter.get("title", ""),
                "summary": chapter.get("summary", ""),
                "lessons": lessons,
            }
        )
    return tree


def find_lesson_dir(course_dir: str | Path, lesson_id: str) -> Path | None:
    """按 ``L1-1`` 找到 ``chapters/<章>/lessons/L1-1-<标题>/`` 目录。"""
    directory = Path(course_dir)
    chapters = directory / "chapters"
    if not chapters.is_dir():
        return None
    for chapter in sorted(path for path in chapters.iterdir() if path.is_dir()):
        lessons = chapter / "lessons"
        if not lessons.is_dir():
            continue
        for lesson in sorted(path for path in lessons.iterdir() if path.is_dir()):
            if lesson.name == lesson_id or lesson.name.startswith(lesson_id + "-"):
                return lesson
    return None


def scan_courses(courses_dir: str | Path) -> list[CourseSummary]:
    """扫描课程库（文件系统是唯一事实来源，§2.2）。"""
    base = Path(courses_dir)
    if not base.is_dir():
        return []

    summaries: list[CourseSummary] = []
    for course_dir in sorted(path for path in base.iterdir() if path.is_dir()):
        if course_dir.name.startswith("."):
            continue
        data, issues = load_course(course_dir)
        tree = lesson_tree(course_dir) if data else []
        total_lessons = sum(len(chapter["lessons"]) for chapter in tree)
        completed = sum(
            1
            for chapter in tree
            for lesson in chapter["lessons"]
            if lesson.get("status") == "completed"
        )
        summaries.append(
            CourseSummary(
                id=str(data.get("id") or course_dir.name),
                name=str(data.get("name") or course_dir.name),
                path=course_dir,
                description=str(data.get("description") or ""),
                tags=tuple(str(tag) for tag in (data.get("tags") or [])),
                theme=data.get("theme"),
                persona=data.get("persona"),
                seed=bool(data.get("seed")),
                pinned=bool(data.get("pinned")),
                sort_index=int(data.get("sort_index") or 0),
                chapters=len(tree),
                lessons=total_lessons,
                completed_lessons=completed,
                broken=bool(issues),
                issues=tuple(issues),
            )
        )
    summaries.sort(key=lambda item: (not item.pinned, item.sort_index, item.name))
    return summaries


def find_course(courses_dir: str | Path, key: str) -> CourseSummary | None:
    for summary in scan_courses(courses_dir):
        if key in (summary.id, summary.name):
            return summary
    return None

# --- 课程操作（FR-04：重命名 / 删除 / 标记完成 / 收藏 / 排序） -------------
def now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def update_course_meta(course_dir: str | Path, **changes: object) -> dict:
    """更新 ``course.json``（原子写）。"""
    from ..infra import file_repository

    path = Path(course_dir) / "course.json"
    meta = read_json(path, default={}) or {}
    meta.update({key: value for key, value in changes.items() if value is not None})
    meta["updated_at"] = now_iso()
    file_repository.write_json(path, meta)
    return meta


def rename_course(course_dir: str | Path, new_name: str, *, index: object | None = None) -> Path:
    """重命名课程：目录 + course.json + 索引同步；失败自动回滚（AC-64）。"""
    from ..core.paths import safe_component
    from ..infra import file_repository

    source = Path(course_dir)
    target = source.parent / safe_component(new_name)
    if target.exists() and target != source:
        _, target = file_repository.unique_course_dir(source.parent, target.name)
    original_meta = read_json(source / "course.json", default={}) or {}

    source.rename(target)
    try:
        update_course_meta(target, name=target.name)
    except Exception:
        target.rename(source)
        file_repository.write_json(source / "course.json", original_meta)
        raise

    if index is not None and hasattr(index, "upsert_course"):
        index.upsert_course(
            course_id=str(original_meta.get("id") or target.name),
            name=target.name,
            path=str(target),
            description=str(original_meta.get("description") or ""),
        )
    return target


def delete_course(course_dir: str | Path, *, index: object | None = None) -> bool:
    """删除课程（走回收站，AC-15）。"""
    from ..infra import file_repository

    target = Path(course_dir)
    meta = read_json(target / "course.json", default={}) or {}
    ok = file_repository.delete_to_recycle_bin(target)
    if ok and index is not None and hasattr(index, "delete_course"):
        index.delete_course(str(meta.get("id") or target.name))
    return ok


def mark_course_complete(course_dir: str | Path, *, completed: bool = True) -> dict:
    """整门课程标记完成 / 取消完成：只改状态，不删内容（AC-63）。"""
    from . import lesson_service

    status = lesson_service.STATUS_COMPLETED if completed else lesson_service.STATUS_IN_PROGRESS
    for lesson_dir in lesson_service.iter_lesson_dirs(course_dir):
        lesson_service.set_lesson_status(lesson_dir, status)
    return lesson_service.sync_course_progress(course_dir)


def toggle_favorite(lesson_dir: str | Path, value: bool | None = None) -> bool:
    """收藏标记（§18.6：``lesson.favorite`` 是标记，不是状态）。"""
    from ..infra import file_repository

    path = Path(lesson_dir) / "lesson.json"
    lesson = read_json(path, default={}) or {}
    lesson["favorite"] = bool(lesson.get("favorite")) if value is None else bool(value)
    file_repository.write_json(path, lesson)
    return bool(lesson["favorite"])


def set_pinned(course_dir: str | Path, pinned: bool) -> dict:
    return update_course_meta(course_dir, pinned=bool(pinned))


def set_sort_index(course_dir: str | Path, index_value: int) -> dict:
    return update_course_meta(course_dir, sort_index=int(index_value))