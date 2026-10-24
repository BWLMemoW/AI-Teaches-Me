"""课程总监 Agent（《项目文档》§8.2、《技术文档》§4.4.5）。

两阶段：先**澄清**（≤5 个可跳过的问题），再**生成大纲**（严格 JSON + Schema 校验 +
一次修复重试 + 必要的「细化」轮次）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..core.errors import TruncatedOutput
from ..core.models import GOAL_LABELS, CourseOutline
from ..llm.schema import complete_json, json_schema_of
from .base import AgentBase, PromptAssembler

MIN_CHAPTERS = 3

## 课程目标取向：同一批知识点，四种拆法与讲法完全不同
GOAL_HINTS: dict[str, str] = {
    "general": "通用：结构与篇幅平衡，兼顾直觉与公式。",
    "exam": "应试：以考点与题型组织小节；每章末给自测与易错点；给可复用的解题步骤；先讲判别条件与常见陷阱。",
    "crash": "速成：只保留最短可用路径（3-5 小时可学完）；砍掉历史沿革与繁杂推导；每节给最小可懂例子；标注可后续深入的扩展点。",
    "interest": "兴趣：故事与生活例子优先；少公式多直觉；允许留白与悬念。",
    "deep_dive": "精讲：推导完整、边界条件与反例齐全；与相邻知识建立联系；每节给延伸阅读与思考题。",
    "review": "复习巩固：先给知识地图与速查表，再按遗忘曲线安排回顾；每节提示「上次学到哪」与易错点复现。",
    "practice": "刷题实战：以题组驱动，先给判别条件与套路，再给变式与常见坑；每题标注考查的知识点。",
    "career": "求职面试：围绕高频面试题与项目讲述组织；给「怎么讲给面试官听」的话术框架与追问准备。",
    "research": "学术研究：给定义与符号体系、证明思路、相关工作与论文导读；强调适用范围与未解决问题。",
    "beginner": "零基础：先补前置概念，术语第一次出现就解释；多用生活类比；每步都给可自检的小练习。",
    "advanced": "进阶提升：跳过基础铺垫，直接给深层原理、性能/数值细节与工程取舍；配更高难度的思考题。",
    "project": "项目实战：以可运行的最小项目为主线，边做边讲；给排错清单、验收标准与扩展方向。",
}
MIN_LESSONS_PER_CHAPTER = 2


def goal_prompt_line(goal_type: str = "general", goal_types: Sequence[str] | None = None) -> str:
    """把（可多选的）课程目标拼成提示词里的一行。"""
    chosen = [item for item in (goal_types or []) if item] or ([goal_type] if goal_type else ["general"])
    labels = "、".join(GOAL_LABELS.get(item, item) for item in chosen)
    hints = " ".join(GOAL_HINTS.get(item, "") for item in chosen if item in GOAL_HINTS)
    return f"课程目标：{labels}｜{hints}"


MATERIALS_JSON_LIMIT = 12000  # JSON 超过这个长度就改送紧凑文本大纲（不截断）
MATERIALS_MAX_TOKENS = 32768  # 资料文档很长：一次写不完就整段重试时用的预算

MATERIALS_PROMPT = (
    "请基于以下教学大纲，写一份**课程资料文档**（Markdown）。要求："
    "① 开头给课程定位、适用人群与该目标取向下最该抓住的主线；"
    "② 按章 → 小节 → 知识点展开，每个知识点给出：一句话定义、关键要点（2-4 条）、一个最小例子或反例、常见误区；"
    "③ 小节末标注前置小节与该节必须掌握的知识点；"
    "④ 只输出 Markdown 正文，不要代码围栏、不要解释。"
)


def compact_outline_text(outline: CourseOutline) -> str:
    """大纲的紧凑文本版：JSON 太长时用它，保证提示词里没有半截结构。"""
    lines = [f"课程时长：约 {outline.estimated_hours} 小时"]
    if outline.learning_goals:
        lines.append("学习目标：" + "；".join(str(item) for item in outline.learning_goals))
    for chapter in outline.chapters:
        head = f"\n{chapter.id}  {chapter.title}"
        lines.append(f"{head}：{chapter.summary}" if chapter.summary else head)
        for lesson in chapter.lessons:
            points = "、".join(
                f"{point.title}（{point.difficulty}）" for point in lesson.knowledge_points
            )
            lines.append(f"  - {lesson.id} {lesson.title}（{lesson.estimated_minutes} 分钟）：{points}")
    return "\n".join(lines)


def build_materials_prompt(course_name: str, outline: CourseOutline, goal_type: str = "general", goal_types: Sequence[str] | None = None) -> str:
    """把大纲回灌给模型，要求产出「课程资料文档」（知识点详解版）。

    JSON 太长时**不截断**（截出来的非法 JSON 会让模型只思考不落笔），
    改用等价的紧凑文本大纲，保证送进去的永远是完整结构。
    """
    import json as _json

    payload = _json.dumps(outline.model_dump(), ensure_ascii=False)
    if len(payload) <= MATERIALS_JSON_LIMIT:
        body = "【教学大纲 JSON】\n" + payload
    else:
        body = "【教学大纲（结构化摘要）】\n" + compact_outline_text(outline)
    return (
        f"课程名：{course_name}\n"
        + goal_prompt_line(goal_type, goal_types)
        + "\n\n" + MATERIALS_PROMPT + "\n\n" + body
    )

class DirectorAgent(AgentBase):
    name = "director"
    skeleton_prompt = "director/skeleton.md"
    protocol_prompt = "director/protocol.md"
    default_task = "outline"

    def __init__(self, client: Any, *, assembler: PromptAssembler | None = None, refine: bool = True) -> None:
        super().__init__(client, assembler=assembler)
        self.refine = refine

    # --- 提示词 ---------------------------------------------------------
    def system_prompt(self, *, phase: str = "generate", **overrides: Any) -> str:
        if phase == "clarify":
            from .base import PromptParts

            return self.assembler.assemble(
                PromptParts(skeleton="director/skeleton.md", protocol="director/clarify.md", coverage=None)
            )
        return super().system_prompt(**overrides)

    # --- 阶段 1：澄清 ---------------------------------------------------
    async def clarify(
        self,
        *,
        course_name: str,
        user_outline: str = "",
        material_summary: str = "",
        cancel: Any = None,
    ) -> AsyncIterator[str]:
        user_prompt = (
            f"课程名：{course_name}\n"
            f"用户提供的大纲（可能为空）：{user_outline or '（无）'}\n"
            f"资料摘要（可能为空）：{material_summary or '（无）'}\n\n"
            "请给出不超过 5 个关键澄清问题。"
        )
        messages = [{"role": "user", "content": user_prompt}]
        async for piece in self.stream(
            messages, system=self.system_prompt(phase="clarify"), cancel=cancel, task="outline"
        ):
            yield piece

    # --- 阶段 2：生成大纲 -----------------------------------------------
    def build_user_prompt(
        self,
        *,
        course_name: str,
        user_outline: str = "",
        material_summary: str = "",
        clarify_answers: str = "",
        goal_type: str = "general",
        goal_types: Sequence[str] | None = None,
    ) -> str:
        return (
            f"课程名：{course_name}\n"
            f"用户提供的大纲（可能为空）：{user_outline or '（无）'}\n"
            f"资料摘要（可能为空）：{material_summary or '（无）'}\n"
            f"澄清回答（可能为空）：{clarify_answers or '（用户选择跳过）'}\n\n"
            + goal_prompt_line(goal_type, goal_types) + "\n\n"
            + "请按输出协议给出完整教学大纲 JSON。"
        )

    @staticmethod
    def needs_refinement(outline: CourseOutline) -> bool:
        """章节数过少 / 粒度过粗 → 需要一次细化轮次（§4.4.5）。"""
        if len(outline.chapters) < MIN_CHAPTERS:
            return True
        for chapter in outline.chapters:
            if len(chapter.lessons) < MIN_LESSONS_PER_CHAPTER:
                return True
            for lesson in chapter.lessons:
                if not lesson.knowledge_points:
                    return True
        return outline.total_lessons() == 0

    async def generate(
        self,
        *,
        course_name: str,
        user_outline: str = "",
        material_summary: str = "",
        clarify_answers: str = "",
        goal_type: str = "general",
        goal_types: Sequence[str] | None = None,
        cancel: Any = None,
    ) -> CourseOutline:
        messages: list[Mapping[str, Any]] = [
            {
                "role": "user",
                "content": self.build_user_prompt(
                    course_name=course_name,
                    user_outline=user_outline,
                    material_summary=material_summary,
                    clarify_answers=clarify_answers,
                    goal_types=goal_types,
                ),
            }
        ]
        outline = await complete_json(
            self.client,
            self.with_system(messages, self.system_prompt(phase="generate")),
            model_cls=CourseOutline,
            task="outline",
            cancel=cancel,
            json_schema=json_schema_of(CourseOutline, name="course_outline"),
        )

        if self.refine and self.needs_refinement(outline):
            refined = await self._refine(outline, cancel=cancel)
            if refined is not None:
                outline = refined
        return outline

    async def generate_materials(
        self,
        *,
        course_name: str,
        outline: CourseOutline,
        goal_type: str = "general",
        goal_types: Sequence[str] | None = None,
        cancel: Any = None,
    ) -> str:
        """生成《课程资料》Markdown（知识点详解）；调用方负责失败兜底。

        资料**不会边生成边给用户看**，所以被 token 上限截断时直接整段重试一次
        （更大预算），不会产生重复文本。
        """
        prompt = build_materials_prompt(course_name, outline, goal_type, goal_types)
        messages: list[Mapping[str, Any]] = [{"role": "user", "content": prompt}]

        async def collect(**kwargs: Any) -> str:
            parts: list[str] = []
            async for piece in self.stream(messages, task="outline", cancel=cancel, **kwargs):
                parts.append(piece)
            return "".join(parts).strip()

        try:
            return await collect()
        except TruncatedOutput:
            return await collect(max_tokens=MATERIALS_MAX_TOKENS)
    async def _refine(self, outline: CourseOutline, *, cancel: Any = None) -> CourseOutline | None:
        # 不截断：截出来的半截 JSON 会让模型只顾着补括号、不落笔（与资料生成同一策略）
        payload = outline.model_dump_json()
        draft = payload if len(payload) <= MATERIALS_JSON_LIMIT else compact_outline_text(outline)
        messages: list[Mapping[str, Any]] = [
            {"role": "user", "content": "这是当前大纲草稿，请细化它。"},
            {"role": "assistant", "content": draft},
            {
                "role": "user",
                "content": (
                    f"请细化：至少 {MIN_CHAPTERS} 章，每章至少 {MIN_LESSONS_PER_CHAPTER} 个小节，"
                    "每个小节都要有知识点（id 用 kp-xxx）。只输出完整 JSON，不要解释。"
                ),
            },
        ]
        try:
            return await complete_json(
                self.client,
                self.with_system(messages, self.system_prompt(phase="generate")),
                model_cls=CourseOutline,
                task="outline",
                cancel=cancel,
                json_schema=json_schema_of(CourseOutline, name="course_outline"),
            )
        except Exception:  # noqa: BLE001 - 细化失败就保留原草稿，不阻塞用户
            return None
