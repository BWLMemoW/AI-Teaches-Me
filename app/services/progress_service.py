"""进度服务（《项目文档》FR-09 / FR-18、§3.5.4；《技术文档》§3.5.4）。

包含：掌握度、学习时长、连续学习天数、复习队列（简化版 SM-2）、
以及**测验 / 自评三级三态**的统一裁决（小节 > 课程 > 全局）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..core.models import LessonProgressEntry, ProgressMeta, ReviewItem
from ..core.paths import read_json
from . import lesson_service

REVIEW_INTERVALS = (1, 3, 7, 14, 30)
LOW_MASTERY = 2


def today() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return today().isoformat(timespec="seconds")


@dataclass
class CourseStats:
    total_lessons: int = 0
    completed_lessons: int = 0
    total_minutes: int = 0
    streak_days: int = 0
    review_due: int = 0
    average_mastery: float | None = None

    @property
    def ratio(self) -> float:
        if self.total_lessons <= 0:
            return 0.0
        return min(1.0, self.completed_lessons / self.total_lessons)


def resolve_assessment(
    *,
    lesson_quiz: str,
    lesson_self: str,
    course_quiz: str,
    course_self: str,
    global_quiz: bool,
    global_self: bool,
) -> dict[str, bool]:
    """三级三态裁决：小节 > 课程 > 全局（FR-18 / §18.6）。"""

    def decide(local: str, course: str, default: bool) -> bool:
        if local == "on":
            return True
        if local == "off":
            return False
        if course == "on":
            return True
        if course == "off":
            return False
        return bool(default)

    return {
        "quiz": decide(lesson_quiz, course_quiz, global_quiz),
        "self_assessment": decide(lesson_self, course_self, global_self),
    }


class ProgressService:
    def __init__(self, course_dir: str | Path) -> None:
        self.course_dir = Path(course_dir)

    # --- 读取 -----------------------------------------------------------
    @property
    def progress_file(self) -> Path:
        return self.course_dir / "progress.json"

    def load(self) -> ProgressMeta:
        raw = read_json(self.progress_file, default={}) or {}
        return ProgressMeta.model_validate(raw) if raw else ProgressMeta()

    # --- 写入 -----------------------------------------------------------
    def update_lesson(
        self,
        lesson_dir: str | Path,
        *,
        status: str,
        mastery: int | None = None,
        quiz_score: float | None = None,
        minutes: int | None = None,
    ) -> dict:
        """更新小节状态并同步 ``progress.json``；掌握度低时自动进复习队列。"""
        lesson = lesson_service.set_lesson_status(lesson_dir, status, mastery=mastery)
        if minutes is not None:
            lesson["time_spent_seconds"] = int(lesson.get("time_spent_seconds") or 0) + int(minutes) * 60
            from ..infra import file_repository

            file_repository.write_json(Path(lesson_dir) / "lesson.json", lesson)
        if quiz_score is not None:
            lesson["quiz_score"] = quiz_score
            from ..infra import file_repository

            file_repository.write_json(Path(lesson_dir) / "lesson.json", lesson)

        progress = lesson_service.sync_course_progress(self.course_dir)
        lesson_id = str(lesson.get("id") or Path(lesson_dir).name.split("-")[0])
        if mastery is not None:
            self.schedule_review(lesson_id, mastery)
        return progress

    def set_mastery(self, lesson_dir: str | Path, mastery: int) -> dict:
        return self.update_lesson(lesson_dir, status=lesson_service.STATUS_COMPLETED, mastery=mastery)

    # --- 复习队列（简化版 SM-2） ----------------------------------------
    def schedule_review(self, lesson_id: str, mastery: int) -> list[ReviewItem]:
        progress = self.load()
        queue = [item for item in progress.review_queue if item.lesson_id != lesson_id]
        if mastery <= LOW_MASTERY:
            existing = next((item for item in progress.review_queue if item.lesson_id == lesson_id), None)
            interval_index = 0
            if existing is not None:
                interval_index = min(
                    len(REVIEW_INTERVALS) - 1,
                    REVIEW_INTERVALS.index(existing.interval_days) + 1 if existing.interval_days in REVIEW_INTERVALS else 1,
                )
            interval = REVIEW_INTERVALS[interval_index]
            queue.append(
                ReviewItem(
                    lesson_id=lesson_id,
                    mastery=mastery,
                    due_at=(today() + timedelta(days=interval)).date().isoformat(),
                    interval_days=interval,
                )
            )
        progress.review_queue = queue
        progress.updated_at = now_iso()
        self._save(progress)
        return queue

    def exclude_from_review(self, lesson_id: str) -> list[ReviewItem]:
        progress = self.load()
        for item in progress.review_queue:
            if item.lesson_id == lesson_id:
                item.excluded = True
        progress.updated_at = now_iso()
        self._save(progress)
        return progress.review_queue

    def due_reviews(self) -> list[ReviewItem]:
        today_text = today().date().isoformat()
        return [
            item
            for item in self.load().review_queue
            if not item.excluded and (item.due_at or "") <= today_text
        ]

    # --- 统计 -----------------------------------------------------------
    def stats(self) -> CourseStats:
        progress = self.load()
        entries = list(progress.lessons.values())
        masteries = [item.mastery for item in entries if item.mastery]
        return CourseStats(
            total_lessons=len(entries),
            completed_lessons=sum(1 for item in entries if item.status == lesson_service.STATUS_COMPLETED),
            total_minutes=sum(int(item.minutes or 0) for item in entries),
            streak_days=self.streak_days(),
            review_due=len(self.due_reviews()),
            average_mastery=(sum(masteries) / len(masteries)) if masteries else None,
        )

    def streak_days(self) -> int:
        """连续学习天数：按进度文件里记录的学习日期去重倒推（简化实现）。"""
        progress = self.load()
        days = sorted({str(progress.updated_at)[:10]} | {
            str(item.get("date")) for item in [] if isinstance(item, dict)
        })
        if not days:
            return 0
        streak = 1
        cursor = datetime.fromisoformat(days[-1]).date() if days[-1] else today().date()
        for previous in reversed(days[:-1]):
            try:
                day = datetime.fromisoformat(previous).date()
            except ValueError:
                continue
            if (cursor - day).days == 1:
                streak += 1
                cursor = day
            else:
                break
        return streak

    # --- 内部 -----------------------------------------------------------
    def _save(self, progress: ProgressMeta) -> None:
        from ..infra import file_repository

        file_repository.write_json(self.progress_file, progress.model_dump(mode="json"))
