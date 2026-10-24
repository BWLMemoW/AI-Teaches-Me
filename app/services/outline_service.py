"""大纲服务（《技术文档》§4.3、《项目文档》§7.3 关键流程：课程创建）。

服务是事务边界：**草稿 → 澄清 → 生成 → 校验 → 提交落盘**都在这里完成，
Agent 只负责「读上下文 → 生成」，UI 只负责展示与编辑（§4.2 / §4.3）。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    LessonPersonaRef,
    LessonProgressEntry,
    LessonVersion,
    ProgressMeta,
    outline_to_markdown,
)
from ..core.paths import safe_component, safe_title_component
from ..infra import file_repository
from ..infra.sqlite_index import SqliteIndex
from .lesson_service import SETTINGS_VERSION
from .material_service import MaterialService, build_material_digest

LOGGER = logging.getLogger(__name__)

#: 分级摘要缓存目录（按资料内容校验和缓存，避免重复花钱）
MATERIAL_SUMMARY_CACHE_DIR = "material_summary"
MATERIAL_SUMMARY_CACHE_VERSION = 1
#: 单份资料进模型前先压到这个量级；仍过长就 map-reduce
MATERIAL_SUMMARY_MAX_CHARS = 6000
#: 没配生成偏好时的默认摘要预算
DEFAULT_MATERIAL_BUDGET_CHARS = 24000


def _cancelled(token: Any) -> bool:
    return bool(token is not None and getattr(token, "cancelled", False))


def _emit(callback: Any, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:  # noqa: BLE001 - 进度回调失败不能影响资料预读
        pass


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


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
    materials_error: str = ""  # 资料生成失败原因（提交时用本地兜底，仍会写出课程资料）
    material_paths: list[str] = field(default_factory=list)  # 用户上传的资料原件路径（提交时正式入库）
    material_report: list[dict] = field(default_factory=list)  # 资料体检报告
    material_stats: dict[str, Any] = field(default_factory=dict)  # 覆盖情况（页数 / 知识图数）
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
                "material_paths": list(self.material_paths),
        }


@dataclass
class CreateResult:
    course_id: str
    name: str
    course_dir: Path
    outline: CourseOutline
    files: list[Path] = field(default_factory=list)



DIFFICULTY_LABELS = {"beginner": "入门", "intermediate": "进阶", "advanced": "挑战"}


def _materials_manifest_section(report: Sequence[dict]) -> str:
    """《课程资料》末尾的资料清单：让用户在应用里就能看到"资料读到了什么"。"""
    lines = ["## 资料清单", "", "> 建课时读取的原始资料与解析结果（原件在 `materials/raw/`，解析稿在 `materials/parsed/`）。", ""]
    for item in report:
        name = str(item.get("filename") or "（未命名）")
        kind = str(item.get("kind") or "")
        status = str(item.get("status") or "")
        if status != "parsed":
            lines.append(f"- **{name}**（{kind}）：解析失败——{item.get('error') or '原因未知'}")
            continue
        bits = [
            f"{item.get('pages') or 0} 页",
            f"{item.get('chunks') or 0} 段",
        ]
        if item.get("tables"):
            bits.append(f"{item['tables']} 个表格")
        if item.get("images"):
            bits.append(
                f"{item.get('images') or 0} 张图（读取 {item.get('images_read') or 0}，"
                f"跳过装饰图 {item.get('images_skipped_decor') or 0}）"
            )
        if item.get("pages_without_text"):
            bits.append(f"无文字页 {item['pages_without_text']}")
        if item.get("vision_skipped"):
            bits.append("本次未做图片识别")
        lines.append(f"- **{name}**（{kind}）：" + "；".join(bits))
        headings = [str(text) for text in (item.get("headings") or [])[:8]]
        if headings:
            lines.append(f"  - 覆盖章节：{' / '.join(headings)}")
    return "\n".join(lines)


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
        material_budget_chars: int = DEFAULT_MATERIAL_BUDGET_CHARS,
    ) -> None:
        self.courses_dir = Path(courses_dir)
        self.client = client
        self.index = index
        self.director = director or DirectorAgent(client)
        self.material_budget_chars = int(material_budget_chars or DEFAULT_MATERIAL_BUDGET_CHARS)

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
        material_paths: Sequence[str] = (),
        material_report: Sequence[dict] = (),
        material_stats: dict[str, Any] | None = None,
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
            material_paths=[str(item) for item in material_paths],
            material_report=[dict(item) for item in material_report],
            material_stats=dict(material_stats or {}),
        )

    # --- 资料预读（分级摘要） ---------------------------------------------
    def preview_materials(
        self,
        files: Sequence[str | Path],
        *,
        workspace: str | Path,
        cancel: Any = None,
        on_event: Any = None,
        confirm: Any = None,
        vision: Any = None,
        vision_batch: Any = None,
        budget_chars: int | None = None,
    ) -> dict[str, Any]:
        """建课前「读全读懂」资料：解析（含知识图识图）→ 分级摘要 → 覆盖统计。

        返回 ``{"summary", "report", "stats", "materials"}``；summary 直接塞进草稿的
        ``material_summary``，成为总监 Agent 的上下文。
        """
        budget = int(budget_chars or self.material_budget_chars)
        cache_dir = Path(workspace) / "data" / "cache"
        staging = cache_dir / "draft_materials"
        # 没显式注入就用客户端自带的读图能力（少一处"忘了传就静默不读图"的坑）
        injected = vision is not None
        vision = vision or self.default_vision(cancel=cancel)
        if vision_batch is None and not injected:
            vision_batch = self.default_vision_batch(cancel=cancel)
        service = MaterialService(
            course_dir=staging,
            index=None,
            cache_dir=cache_dir,
            vision=vision,
            vision_batch=vision_batch,
            cancel=cancel,
            on_event=on_event,
            confirm=confirm,
        )
        materials = service.parse_files([str(item) for item in files])
        digest = build_material_digest(materials, budget_chars=budget)
        # 用户按了「停止识别」就不要再偷偷跑目录级摘要：正文已经解析好，
        # 摘要可以先退回本地版本，不能让界面又等一两分钟。
        if not _cancelled(cancel):
            _emit(
                on_event,
                {
                    "stage": "digest",
                    "file": "整理资料摘要",
                    "index": 1,
                    "total": max(1, len(materials)),
                },
            )
            digest = self._upgrade_digest(
                materials, digest, cache_dir=cache_dir, cancel=cancel, budget_chars=budget
            )
        stats = {
            "files": len(materials),
            "pages": sum(item.pages for item in materials),
            "chunks": sum(len(item.chunks) for item in materials),
            "tables": sum(item.tables for item in materials),
            "figures_read": sum(item.vision_called for item in materials),
            "figures_skipped": sum(item.dropped_images for item in materials),
            "vision_skipped": any(item.vision_skipped for item in materials),
            "cancelled": _cancelled(cancel),
            "budget_chars": budget,
        }
        return {
            "summary": digest,
            "report": [item.report() for item in materials],
            "stats": stats,
            "materials": materials,
        }

    def default_vision(self, *, cancel: Any = None) -> Any:
        """模型原生读图能力 → 同步回调（服务层只用客户端抽象，不碰 SDK）。"""
        client = getattr(self, "client", None)
        if client is None or not hasattr(client, "review_image"):
            return None

        def recognize(png: bytes, prompt: str) -> str:
            return asyncio.run(
                client.review_image(png, prompt=prompt, task="vision_review", cancel=cancel)
            )

        return recognize

    def default_vision_batch(self, *, cancel: Any = None) -> Any:
        """多图打包读图：一次消息里放几张（几百页扫描件靠这个把调用量按批压下来）。"""
        client = getattr(self, "client", None)
        if client is None or not hasattr(client, "stream_chat"):
            return None

        def recognize_many(images: list[bytes], prompt: str) -> str:
            return asyncio.run(self._call_vision_many(images, prompt, cancel=cancel))

        return recognize_many

    async def _call_vision_many(self, images: Sequence[bytes], prompt: str, *, cancel: Any = None) -> str:
        import base64

        client = getattr(self, "client", None)
        if client is None:
            return ""
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for data in images:
            encoded = base64.b64encode(data).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{_image_mime(data)};base64,{encoded}"}}
            )
        parts: list[str] = []
        async for piece in client.stream_chat(
            [{"role": "user", "content": content}],
            task="vision_review",
            cancel=cancel,
            # 一批 4 张图失败时，重试整批一分钟往往比退回逐张更慢。
            attempts=1,
        ):
            parts.append(str(piece))
        return "".join(parts)

    def _upgrade_digest(
        self,
        materials: Sequence[Any],
        digest: str,
        *,
        cache_dir: Path,
        cancel: Any = None,
        budget_chars: int = DEFAULT_MATERIAL_BUDGET_CHARS,
    ) -> str:
        """第 3 层摘要：有可用档案时，对每份资料做一次「目录级摘要 + 关键词表」调用。"""
        client = getattr(self, "client", None)
        if client is None or not digest:
            return digest
        cache_root = cache_dir / MATERIAL_SUMMARY_CACHE_DIR
        parsed = [item for item in materials if getattr(item, "status", "") == "parsed"]
        if not parsed:
            return digest
        share = max(1200, budget_chars // len(parsed))

        def summarize(item: Any) -> tuple[Any, str, str]:
            local = build_material_digest([item], budget_chars=MATERIAL_SUMMARY_MAX_CHARS)
            if _cancelled(cancel):
                return item, local, ""
            summary = self._cached_material_summary(item, local, cache_root=cache_root, cancel=cancel)
            return item, local, summary

        summaries: dict[int, tuple[str, str]] = {}
        workers = max(1, min(3, len(parsed)))
        if workers == 1:
            for item in parsed:
                _item, local, summary = summarize(item)
                summaries[id(_item)] = (local, summary)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atm-digest") as pool:
                futures = [pool.submit(summarize, item) for item in parsed]
                for future in as_completed(futures):
                    try:
                        item, local, summary = future.result()
                    except Exception as exc:  # noqa: BLE001 - 摘要失败退回本地内容
                        LOGGER.warning("资料目录级摘要失败（改用本地摘要）：%s", exc)
                        continue
                    summaries[id(item)] = (local, summary)

        sections: list[str] = []
        for item in parsed:
            local, summary = summaries.get(
                id(item),
                (build_material_digest([item], budget_chars=MATERIAL_SUMMARY_MAX_CHARS), ""),
            )
            body = summary or local
            section = (
                f"### 资料 [{item.material_id}] {item.filename}"
                f"（{item.kind}，{item.pages} 页）\n{body}"
            )
            if summary:
                # 目录级摘要之外再带一小段本地原文节选，别让模型只看到"转述"
                section += "\n\n（本地摘要节选）\n" + local[:600]
            sections.append(section[:share])
        text = "\n\n".join(sections)
        return text[:budget_chars] if len(text) > budget_chars else text

    def _cached_material_summary(self, item: Any, local: str, *, cache_root: Path, cancel: Any = None) -> str:
        if _cancelled(cancel):
            return ""
        checksum = str(getattr(item, "checksum", "") or "")
        path = cache_root / f"{checksum}-v{MATERIAL_SUMMARY_CACHE_VERSION}.json" if checksum else None
        if path is not None and path.exists():
            payload = file_repository.read_json_or_default(path, {}) or {}
            cached = str(payload.get("summary") or "").strip()
            if cached:
                return cached
        prompt = (
            "下面是一份学习资料的本地摘要（含目录结构、分段要点与关键词）。"
            "请输出一份**目录级摘要**：① 这份资料整体讲什么、适合什么水平；"
            "② 按章节列出它覆盖的主题（保留原文章节名）；③ 给出一份关键词表（10–20 个）。"
            "只输出正文，不要客套，不要编造资料里没有的内容。\n\n" + local[:MATERIAL_SUMMARY_MAX_CHARS]
        )
        try:
            if _cancelled(cancel):
                return ""
            summary = asyncio.run(self._call_summary(prompt, cancel=cancel))
        except Exception as exc:  # noqa: BLE001 - 摘要失败就用本地摘要，不阻塞建课
            LOGGER.warning("资料目录级摘要失败（改用本地摘要）：%s", exc)
            return ""
        text = str(summary or "").strip()
        if text and path is not None:
            try:
                file_repository.write_json(
                    path, {"version": MATERIAL_SUMMARY_CACHE_VERSION, "summary": text}
                )
            except OSError:
                pass
        return text

    async def _call_summary(self, prompt: str, *, cancel: Any = None) -> str:
        client = getattr(self, "client", None)
        if client is None:
            return ""
        parts: list[str] = []
        async for piece in client.stream_chat(
            [{"role": "user", "content": prompt}], task="outline", cancel=cancel
        ):
            parts.append(str(piece))
        return "".join(parts)

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
        # 一并生成《课程资料》：失败**不阻塞建课**（commit 会用本地兜底写出文档），
        # 但必须记下原因，别让用户以为「AI 写了资料」其实一个字都没有。
        try:
            text = await self.director.generate_materials(
                course_name=draft.name,
                outline=outline,
                goal_type=getattr(draft, "goal_type", "general"),
                goal_types=list(getattr(draft, "goal_types", []) or []),
                cancel=cancel,
            )
        except Exception as exc:  # noqa: BLE001 - 见上：记录但不阻塞
            draft.materials_error = str(exc) or type(exc).__name__
            LOGGER.warning("课程资料生成失败，提交时用本地兜底：%s", exc)
        else:
            if text.strip():
                draft.materials_md = text
                draft.materials_error = ""
            else:
                draft.materials_error = "模型没有返回课程资料内容"
                LOGGER.warning("课程资料为空，提交时用本地兜底")
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

        # 用户上传的资料正式进课程：原件 → materials/raw，解析稿 → materials/parsed，
        # 清单逐条登记，分块进索引（这一步以前缺失，导致「传了资料但课程里什么都没有」）。
        report: list[dict] = list(getattr(draft, "material_report", []) or [])
        import_result = None
        material_paths = [str(item) for item in (getattr(draft, "material_paths", []) or []) if str(item).strip()]
        if material_paths:
            service = MaterialService(
                course_dir=course_dir,
                index=self.index,
                cache_dir=Path(self.courses_dir).parent / "data" / "cache",
            )
            try:
                import_result = service.import_files(material_paths, course_id=draft.course_id)
            except Exception as exc:  # noqa: BLE001 - 资料入库失败不该毁掉整门课
                LOGGER.warning("资料入库失败：%s", exc)
                draft.materials_error = draft.materials_error or f"资料入库失败：{exc}"
            else:
                if import_result.report:
                    report = import_result.report
                written.extend(import_result.files)
        if report:
            materials_md = materials_md.rstrip() + "\n\n" + _materials_manifest_section(report)
            file_repository.write_text(course_dir / "materials" / "课程资料.md", materials_md)

        progress = ProgressMeta(course_id=draft.course_id)
        for chapter in target_outline.chapters:
            chapter_title = safe_title_component(f"{chapter.id}-{chapter.title}")
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
                lesson_title = safe_title_component(f"{lesson.id}-{lesson.title}")
                lesson_dir = lessons_dir / lesson_title
                lesson_dir.mkdir(parents=True, exist_ok=True)
                meta_lesson = LessonMeta(
                    id=lesson.id,
                    title=lesson.title,
                    depth="standard",
                    persona=LessonPersonaRef(id="inherit"),
                    versions=[LessonVersion(version=1, model="", reason="待生成")],
                )
                meta_lesson.current_version = 0
                lesson_payload = meta_lesson.model_dump(mode="json")
                # 新小节默认跟随「设置」里的老师 / 深度；每节仍可单独覆盖。
                lesson_payload["depth"] = "inherit"
                lesson_payload["settings_version"] = SETTINGS_VERSION
                written.append(
                    file_repository.write_json(lesson_dir / "lesson.json", lesson_payload)
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
