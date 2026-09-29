"""领域模型（《项目文档》§8.2 / §9、《技术文档》§3.5 与 §18.6 枚举）。

纯数据结构 + 校验规则（Pydantic），不依赖 Qt、不依赖外部服务（§2.1 依赖方向）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 《项目文档》§18.6 枚举速查
DEPTHS: tuple[str, ...] = ("minimal", "standard", "deep")
LESSON_STATUSES: tuple[str, ...] = ("not_started", "in_progress", "completed", "review_needed")
DIFFICULTIES: tuple[str, ...] = ("beginner", "intermediate", "advanced")
BLOCK_TYPES: tuple[str, ...] = (
    "meta", "text", "callout", "analogy", "diagram", "image", "svg", "animation",
    "code", "quiz", "table", "summary", "next_preview", "ref", "error", "done",
)
CALLOUT_STYLES: tuple[str, ...] = ("hook", "tip", "warn", "question", "fun")
DIAGRAM_KINDS: tuple[str, ...] = ("svg", "mermaid", "ascii")
REF_TYPES: tuple[str, ...] = (
    "lesson_text", "chat_turn", "svg", "notebook", "material_page", "outline", "web",
)
MASTERY_VALUES: tuple[int, ...] = (1, 2, 3)
TRI_STATE: tuple[str, ...] = ("inherit", "on", "off")

# 课程目标取向（M13 新增：影响大纲结构与讲解侧重）
GOAL_TYPES: tuple[str, ...] = ("general", "exam", "crash", "interest", "deep_dive")
GOAL_LABELS: dict[str, str] = {
    "general": "通用",
    "exam": "应试",
    "crash": "速成",
    "interest": "兴趣",
    "deep_dive": "精讲",
}

Depth = Literal["minimal", "standard", "deep"]
Difficulty = Literal["beginner", "intermediate", "advanced"]
TriState = Literal["inherit", "on", "off"]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- 大纲（§3.5.2） ------------------------------------------------------
class KnowledgePoint(Base):
    id: str
    title: str
    summary: str = ""
    difficulty: Difficulty = "beginner"
    must_master: bool = False

    @field_validator("id", "title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("知识点 id / title 不能为空")
        return value.strip()


class OutlineLesson(Base):
    id: str
    title: str
    objectives: list[str] = Field(default_factory=list)
    estimated_minutes: int = 10
    prerequisites: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    knowledge_points: list[KnowledgePoint] = Field(default_factory=list)


class OutlineChapter(Base):
    id: str
    title: str
    summary: str = ""
    lessons: list[OutlineLesson] = Field(default_factory=list)


class CourseOutline(Base):
    schema_version: int = 1
    course_id: str = ""
    learning_goals: list[str] = Field(default_factory=list)
    estimated_hours: float = 0.0
    chapters: list[OutlineChapter] = Field(default_factory=list)
    assessment_plan: list[str] = Field(default_factory=list)

    def total_lessons(self) -> int:
        return sum(len(chapter.lessons) for chapter in self.chapters)

    def all_knowledge_points(self) -> list[KnowledgePoint]:
        return [point for chapter in self.chapters for lesson in chapter.lessons for point in lesson.knowledge_points]

    def lesson(self, lesson_id: str) -> OutlineLesson | None:
        for chapter in self.chapters:
            for item in chapter.lessons:
                if item.id == lesson_id:
                    return item
        return None

    def chapter_of(self, lesson_id: str) -> OutlineChapter | None:
        for chapter in self.chapters:
            if any(item.id == lesson_id for item in chapter.lessons):
                return chapter
        return None


def outline_to_markdown(outline: CourseOutline) -> str:
    """``outline.json`` → ``outline.md``（§3.4：人读版由 JSON 派生）。"""
    lines = ["# 教学大纲", ""]
    if outline.learning_goals:
        lines.append("## 学习目标")
        lines.append("")
        lines.extend(f"- {goal}" for goal in outline.learning_goals)
        lines.append("")
    if outline.estimated_hours:
        lines.append(f"预计总时长：约 {outline.estimated_hours} 小时")
        lines.append("")
    for chapter in outline.chapters:
        lines.append(f"## {chapter.id} {chapter.title}")
        lines.append("")
        if chapter.summary:
            lines.append(chapter.summary)
            lines.append("")
        for lesson in chapter.lessons:
            lines.append(f"- **{lesson.id} {lesson.title}**（约 {lesson.estimated_minutes} 分钟）")
            if lesson.objectives:
                lines.append("  - 学习目标：" + "；".join(lesson.objectives))
            if lesson.knowledge_points:
                lines.append("  - 知识点：" + "；".join(point.title for point in lesson.knowledge_points))
        lines.append("")
    if outline.assessment_plan:
        lines.append("## 检验方式")
        lines.append("")
        lines.extend(f"- {item}" for item in outline.assessment_plan)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- 课程元信息（§3.5.1） -------------------------------------------------
class LearningToggles(Base):
    quiz: TriState = "inherit"
    self_assessment: TriState = "inherit"
    web_search: TriState = "inherit"


class CourseProfile(Base):
    target_audience: str = ""
    goal: str = ""
    goal_type: str = "general"  # 通用 / 应试 / 速成 / 兴趣 / 精讲
    depth: Depth = "standard"
    weekly_minutes: int = 120


class CourseStats(Base):
    chapters: int = 0
    lessons: int = 0
    completed_lessons: int = 0
    total_minutes: int = 0


class CourseBackground(Base):
    type: Literal["image", "color", "gradient"] = "gradient"
    value: str | None = None
    path: str | None = None
    blur: float = 0.0
    dim: float = 0.1


class CourseIcon(Base):
    """课程图标（M8 新增字段）：预设 / 自绘 SVG / AI 生成，全部离线。

    ``value`` 对 ``preset`` 是预设 id（如 ``math``），对 ``svg`` / ``ai`` 是相对课程的路径
    （``assets/icon.svg``）。``color`` 会替换 SVG 里的 ``currentColor``。
    """

    kind: Literal["preset", "svg", "ai"] = "preset"
    value: str = "general"
    color: str = "#E86A92"
    bg: str = "#FFF5F8"

    @field_validator("color", "bg")
    @classmethod
    def _hex_color(cls, value: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
            raise ValueError("颜色必须是 #RRGGBB 形式")
        return text.upper()

    @property
    def is_preset(self) -> bool:
        return self.kind == "preset"


class CourseMeta(Base):
    schema_version: int = 1
    id: str
    name: str
    description: str = ""
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    cover: str | None = None
    icon: CourseIcon | None = None
    theme: str | None = None
    persona: str | None = None
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    sort_index: int = 0
    seed: bool = False
    learning: LearningToggles = Field(default_factory=LearningToggles)
    background: CourseBackground = Field(default_factory=CourseBackground)
    profile: CourseProfile = Field(default_factory=CourseProfile)
    stats: CourseStats = Field(default_factory=CourseStats)
    llm_profile: str | None = None


# --- 小节元信息（§3.5.3） -------------------------------------------------
class LessonVersion(Base):
    version: int
    created_at: str = Field(default_factory=now_iso)
    tokens: int = 0
    model: str = ""
    reason: str = ""


class LessonAssessment(Base):
    quiz: TriState = "inherit"
    self_assessment: TriState = "inherit"


class LessonPersonaRef(Base):
    id: str = "default-teacher"
    version: int = 1


class LessonMeta(Base):
    schema_version: int = 1
    id: str
    title: str
    status: Literal["not_started", "in_progress", "completed", "review_needed"] = "not_started"
    depth: Depth = "standard"
    assessment: LessonAssessment = Field(default_factory=LessonAssessment)
    current_version: int = 1
    versions: list[LessonVersion] = Field(default_factory=list)
    human_edited: bool = False
    mastery: int | None = None
    quiz_score: float | None = None
    started_at: str | None = None
    completed_at: str | None = None
    time_spent_seconds: int = 0
    generated_assets: list[str] = Field(default_factory=list)
    persona: LessonPersonaRef = Field(default_factory=LessonPersonaRef)
    favorite: bool = False

    @field_validator("mastery")
    @classmethod
    def _mastery_range(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value not in MASTERY_VALUES:
            raise ValueError("mastery 只能是 1 / 2 / 3 / null")
        return value


# --- 进度（§3.5.4） -------------------------------------------------------
class LessonProgressEntry(Base):
    status: Literal["not_started", "in_progress", "completed", "review_needed"] = "not_started"
    mastery: int | None = None
    quiz_score: float | None = None
    minutes: int = 0


class ReviewItem(Base):
    lesson_id: str
    mastery: int | None = None
    due_at: str | None = None
    interval_days: int = 1
    excluded: bool = False


class ProgressMeta(Base):
    schema_version: int = 1
    course_id: str = ""
    last_lesson: str | None = None
    streak_days: int = 0
    total_minutes: int = 0
    updated_at: str = Field(default_factory=now_iso)
    lessons: dict[str, LessonProgressEntry] = Field(default_factory=dict)
    review_queue: list[ReviewItem] = Field(default_factory=list)


# --- 内容块协议（§4.4.4） ------------------------------------------------
class ContentBlock(BaseModel):
    """内容块：已知类型做基本校验，未知类型原样保留（老客户端不崩，§4.4.4）。"""

    model_config = ConfigDict(extra="allow")

    type: str

    @field_validator("type")
    @classmethod
    def _known_or_allowed(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("内容块 type 不能为空")
        return value.strip()

    @property
    def is_known(self) -> bool:
        return self.type in BLOCK_TYPES

    def to_markdown(self) -> str:
        data: dict[str, Any] = self.model_dump()
        kind = self.type
        if kind == "text":
            return str(data.get("md", ""))
        if kind == "callout":
            return f"> {data.get('md', '')}"
        if kind == "analogy":
            return f"**类比 · {data.get('title', '')}**\n\n{data.get('md', '')}"
        if kind == "code":
            return f"```{data.get('lang', '')}\n{data.get('code', '')}\n```"
        if kind == "table":
            headers = [str(cell) for cell in data.get("headers", [])]
            rows = data.get("rows", [])
            if not headers:
                return ""
            lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
            lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
            return "\n".join(lines)
        if kind == "summary":
            return "## 小结\n\n" + "\n".join(f"- {point}" for point in data.get("points", []))
        if kind == "next_preview":
            return f"## 下节预告\n\n{data.get('md', '')}"
        if kind == "quiz":
            return f"### 小测验\n\n{data.get('question', '')}"
        if kind == "ref":
            return f"> 依据：{data.get('quote', '')}"
        return ""


# --- 引用卡片（§3.8） ----------------------------------------------------
class RefCard(Base):
    id: str = ""
    type: Literal[
        "lesson_text", "chat_turn", "svg", "notebook", "material_page", "outline", "web"
    ]
    lesson_id: str | None = None
    material_id: str | None = None
    page: int | None = None
    block: int | None = None  # 定稿字段名：block（不使用 block_index，C-05）
    asset: str | None = None
    note: str | None = None
    url: str | None = None
    title: str | None = None
    domain: str | None = None
    accessed_at: str | None = None
    excerpt: str = ""

    @field_validator("accessed_at")
    @classmethod
    def _web_needs_accessed_at(cls, value: str | None, info) -> str | None:  # type: ignore[no-untyped-def]
        if info.data.get("type") == "web" and not value:
            raise ValueError("web 类引用必须带 accessed_at（§3.8）")
        return value
