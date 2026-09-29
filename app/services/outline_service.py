"""大纲服务（《技术文档》§4.3、《项目文档》§7.3 关键流程：课程创建）。

服务是事务边界：**草稿 → 澄清 → 生成 → 校验 → 提交落盘**都在这里完成，
Agent 只负责「读上下文 → 生成」，UI 只负责展示与编辑（§4.2 / §4.3）。
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents.director import MIN_CHAPTERS, MIN_LESSONS_PER_CHAPTER, DirectorAgent
from ..core.models import (
    CourseMeta,
    GOAL_LABELS,
    CourseOutline,
    CourseProfile,
    CourseStats,
    LessonMeta,
    LessonProgressEntry,
    LessonVersion,
    ProgressMeta,
    outline_to_markdown,
)
from ..core.paths import safe_component
from ..infra import file_repository
from ..infra.sqlite_index import SqliteIndex


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_course_id() -> str:
    return f"course-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(2)}"


@dataclass
class OutlineDraft:
    """向导里的可编辑草稿（尚未落盘）。"""

    name: str
    course_id: str = field(default_factory=new_course_id)
    user_outline: str = ""
    material_summary: str = ""
    clarify_questions: str = ""
    clarify_answers: str = ""
    outline: CourseOutline | None = None
    theme: str | None = None
    persona: str | None = None
    icon: object | None = None
    icon_svg: str | None = None
    goal_type: str = "general"
    goal_types: list[str] = field(default_factory=list)
    materials_md: str = ""  # 课程资料文档（大纲对应的详细知识点）
    tags: list[str] = field(default_factory=list)
    llm_profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "course_id": self.course_id,
            "user_outline": self.user_outline,
            "clarify_answers": self.clarify_answers,
            "outline": self.outline.model_dump() if self.outline else None,
            "theme": self.theme,
            "persona": self.persona,
            "tags": list(self.tags),
        }


@dataclass
class CreateResult:
    course_id: str
    name: str
    course_dir: Path
    outline: CourseOutline
    files: list[Path] = field(default_factory=list)



DIFFICULTY_LABELS = {"beginner": "入门", "intermediate": "进阶", "advanced": "挑战"}


def build_materials_from_outline(course_name: str, outline: CourseOutline, goal_label: str = "") -> str:
    """不依赖模型的《课程资料》拼装（离线可用；与模型产出同构）。"""
    lines = [f"# {course_name} · 课程资料", "", "> 由教学大纲生成，供学习时随时查阅（知识点定义 / 要点 / 例子 / 常见误区）。"]
    if goal_label:
        lines += ["", f"课程目标：{goal_label}"]
    if outline.learning_goals:
        lines += ["", "## 课程学习目标", ""] + [f"- {goal}" for goal in outline.learning_goals]
    for chapter in outline.chapters:
        lines += ["", f"## {chapter.id} {chapter.title}", ""]
        if chapter.summary:
            lines += [chapter.summary, ""]
        for lesson in chapter.lessons:
            lines += [f"### {lesson.id} {lesson.title}", ""]
            if lesson.objectives:
                lines += ["**学习目标**", ""] + [f"- {item}" for item in lesson.objectives] + [""]
            if lesson.prerequisites:
                lines += [f"*前置小节：{'、'.join(lesson.prerequisites)}*", ""]
            for point in lesson.knowledge_points:
                lines += [
                    f"#### {point.title}",
                    "",
                    f"- **定义 / 要点**：{point.summary or '（待补充）'}",
                    f"- **难度**：{DIFFICULTY_LABELS.get(point.difficulty, point.difficulty)}"
                    + ("；**必须掌握**" if point.must_master else ""),
                    "- **最小例子 / 练习**：学习台点「生成讲义」后，这里会由教师 Agent 补上具体例题。",
                    "- **常见误区**：容易只记结论而忽略适用条件，做题时先写判别条件再套公式。",
                    "",
                ]
    lines += ["", "## 使用建议", "", "- 先看本文件建立全局印象 → 再进学习台逐节学 → 学完用「生成笔记」沉淀。"]
    return "\n".join(lines).rstrip() + "\n"

class OutlineService:
    def __init__(
        self,
        *,
        courses_dir: str | Path,
        client: Any,
        index: SqliteIndex | None = None,
        director: DirectorAgent | None = None,
    ) -> None:
        self.courses_dir = Path(courses_dir)
        self.client = client
        self.index = index
        self.director = director or DirectorAgent(client)

    # --- 草稿 -----------------------------------------------------------
    def new_draft(
        self,
        name: str,
        *,
        user_outline: str = "",
        material_summary: str = "",
        theme: str | None = None,
        persona: str | None = None,
        icon: object | None = None,
        icon_svg: str | None = None,
        goal_type: str = "general",
        goal_types: Sequence[str] = (),
        tags: Sequence[str] = (),
        llm_profile: str | None = None,
    ) -> OutlineDraft:
        """建草稿：课程名**立刻去重**，避免用户在向导里看到冲突（AC-12）。"""
        clean = safe_component(name)
        unique_name, _ = file_repository.unique_course_dir(self.courses_dir, clean)
        return OutlineDraft(
            name=unique_name,
            user_outline=user_outline,
            material_summary=material_summary,
            theme=theme,
            persona=persona,
            icon=icon,
            icon_svg=icon_svg,
            goal_type=goal_type,
            goal_types=[str(item) for item in goal_types],
            tags=[str(tag) for tag in tags],
            llm_profile=llm_profile,
        )

    # --- 生成 -----------------------------------------------------------
    async def ask_clarify(self, draft: OutlineDraft, *, cancel: Any = None) -> AsyncIterator[str]:
        parts: list[str] = []
        async for piece in self.director.clarify(
            course_name=draft.name,
            user_outline=draft.user_outline,
            material_summary=draft.material_summary,
            cancel=cancel,
        ):
            parts.append(piece)
            yield piece
        draft.clarify_questions = "".join(parts)

    async def generate(self, draft: OutlineDraft, *, cancel: Any = None) -> CourseOutline:
        outline = await self.director.generate(
            course_name=draft.name,
            user_outline=draft.user_outline,
            material_summary=draft.material_summary,
            clarify_answers=draft.clarify_answers,
            goal_type=getattr(draft, "goal_type", "general"),
            goal_types=list(getattr(draft, "goal_types", []) or []),
            cancel=cancel,
        )
        outline.course_id = draft.course_id
        draft.outline = outline
        # 一并生成《课程资料》（失败则留空，commit 时本地兜底）
        try:
            from ..core.models import GOAL_LABELS

            text = await self.director.generate_materials(
                course_name=draft.name,
                outline=outline,
                goal_type=getattr(draft, "goal_type", "general"),
                goal_types=list(getattr(draft, "goal_types", []) or []),
                cancel=cancel,
            )
            if text.strip():
                draft.materials_md = text
        except Exception:  # noqa: BLE001 - 资料生成失败不阻塞建课
            pass
        return outline

    # --- 校验 -----------------------------------------------------------
    @staticmethod
    def validate(outline: CourseOutline) -> list[str]:
        """结构自检（对应 AC-08 的「结构合理」；返回可展示的中文提示）。"""
        issues: list[str] = []
        if not outline.chapters:
            issues.append("大纲里没有任何章节")
            return issues
        if len(outline.chapters) < MIN_CHAPTERS:
            issues.append(f"章节偏少（当前 {len(outline.chapters)} 章，建议至少 {MIN_CHAPTERS} 章）")
        for chapter in outline.chapters:
            if not chapter.lessons:
                issues.append(f"第 {chapter.id} 章没有小节")
            elif len(chapter.lessons) < MIN_LESSONS_PER_CHAPTER:
                issues.append(f"{chapter.id} 小节偏少（当前 {len(chapter.lessons)} 节，建议至少 {MIN_LESSONS_PER_CHAPTER} 节）")
            for lesson in chapter.lessons:
                if not lesson.knowledge_points:
                    issues.append(f"{lesson.id} 没有知识点")
        seen: set[str] = set()
        for point in outline.all_knowledge_points():
            if point.id in seen:
                issues.append(f"知识点 id 重复：{point.id}")
            seen.add(point.id)
        return issues

    # --- 落盘 -----------------------------------------------------------
    def commit(self, draft: OutlineDraft, outline: CourseOutline | None = None) -> CreateResult:
        """把大纲写成完整课程骨架（§3.3 目录结构）。"""
        target_outline = outline or draft.outline
        if target_outline is None:
            raise ValueError("还没有大纲可以提交")

        name, course_dir = file_repository.unique_course_dir(self.courses_dir, draft.name)
        file_repository.ensure_course_dirs(course_dir)
        target_outline.course_id = draft.course_id

        written: list[Path] = []
        resolved_icon = draft.icon
        if draft.icon_svg or draft.icon is not None:
            from .icon_service import IconService

            service = IconService()
            if draft.icon_svg:
                outcome = service.apply_svg(
                    course_dir,
                    draft.icon_svg,
                    color=getattr(draft.icon, "color", "#E86A92"),
                    bg=getattr(draft.icon, "bg", "#FFF5F8"),
                )
            else:
                outcome = service.apply_preset(
                    course_dir,
                    getattr(draft.icon, "value", "general"),
                    color=getattr(draft.icon, "color", "#E86A92"),
                    bg=getattr(draft.icon, "bg", "#FFF5F8"),
                )
            resolved_icon = outcome.icon

        meta = CourseMeta(
            id=draft.course_id,
            name=name,
            icon=resolved_icon,
            theme=draft.theme,
            persona=draft.persona,
            tags=list(draft.tags),
            llm_profile=draft.llm_profile,
            profile=CourseProfile(
                goal_type=(getattr(draft, "goal_types", []) or [getattr(draft, "goal_type", "general")])[0],
                goal_types=list(getattr(draft, "goal_types", []) or [getattr(draft, "goal_type", "general")]),
            ),
            stats=CourseStats(
                chapters=len(target_outline.chapters),
                lessons=target_outline.total_lessons(),
                completed_lessons=0,
                total_minutes=sum(
                    lesson.estimated_minutes
                    for chapter in target_outline.chapters
                    for lesson in chapter.lessons
                ),
            ),
        )
        written.append(file_repository.write_json(course_dir / "course.json", meta.model_dump(mode="json")))
        written.append(file_repository.write_json(course_dir / "outline.json", target_outline.model_dump(mode="json")))
        written.append(file_repository.write_text(course_dir / "outline.md", outline_to_markdown(target_outline)))

        # 课程资料文档（大纲对应的详细知识点）：模型产出优先，本地拼装兜底
        goal_label = GOAL_LABELS.get(getattr(draft, "goal_type", "general"), "")
        materials_md = (getattr(draft, "materials_md", "") or "").strip() or build_materials_from_outline(
            name, target_outline, goal_label
        )
        written.append(file_repository.write_text(course_dir / "materials" / "课程资料.md", materials_md))
        written.append(
            file_repository.write_json(
                course_dir / "materials" / "materials.json",
                {
                    "materials": [
                        {
                            "id": "m-outline",
                            "filename": "课程资料.md",
                            "kind": "md",
                            "pages": 1,
                            "status": "parsed",
                            "source": "outline",
                            "parsed_at": now_iso(),
                            "checksum": "",
                        }
                    ]
                },
            )
        )

        progress = ProgressMeta(course_id=draft.course_id)
        for chapter in target_outline.chapters:
            chapter_title = safe_component(f"{chapter.id}-{chapter.title}")
            chapter_dir = course_dir / "chapters" / chapter_title
            chapter_dir.mkdir(parents=True, exist_ok=True)
            written.append(
                file_repository.write_json(
                    chapter_dir / "chapter.json",
                    {
                        "schema_version": 1,
                        "id": chapter.id,
                        "title": chapter.title,
                        "summary": chapter.summary,
                        "lesson_order": [lesson.id for lesson in chapter.lessons],
                    },
                )
            )
            lessons_dir = chapter_dir / "lessons"
            lessons_dir.mkdir(parents=True, exist_ok=True)
            for lesson in chapter.lessons:
                lesson_title = safe_component(f"{lesson.id}-{lesson.title}")
                lesson_dir = lessons_dir / lesson_title
                lesson_dir.mkdir(parents=True, exist_ok=True)
                meta_lesson = LessonMeta(
                    id=lesson.id,
                    title=lesson.title,
                    depth="standard",
                    versions=[LessonVersion(version=1, model="", reason="待生成")],
                )
                meta_lesson.current_version = 0
                written.append(
                    file_repository.write_json(lesson_dir / "lesson.json", meta_lesson.model_dump(mode="json"))
                )
                written.append(file_repository.write_text(lesson_dir / "notebook.md", f"# {lesson.title} 的笔记\n\n"))
                progress.lessons[lesson.id] = LessonProgressEntry()
        written.append(file_repository.write_json(course_dir / "progress.json", progress.model_dump(mode="json")))

        if self.index is not None:
            self.index.upsert_course(
                course_id=draft.course_id,
                name=name,
                path=str(course_dir),
                total_lessons=target_outline.total_lessons(),
                completed_lessons=0,
                description=meta.description,
            )
            tag_ids = [self.index.add_tag(tag) for tag in draft.tags if str(tag).strip()]
            if tag_ids:
                self.index.set_course_tags(draft.course_id, tag_ids)
        return CreateResult(
            course_id=draft.course_id,
            name=name,
            course_dir=course_dir,
            outline=target_outline,
            files=written,
        )
