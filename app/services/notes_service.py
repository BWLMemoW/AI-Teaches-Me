"""笔记服务（《项目文档》FR-10 / §13.11；《技术文档》§3.3 ``notes/``）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..core.models import ContentBlock
from ..infra import file_repository
from . import lesson_service

NOTE_SECTIONS = ("本节目标", "知识点提纲", "关键例子", "易错点", "我的疑问与结论", "依据与相关")
INCOMPLETE_NOTICE = "> 本小节尚未学完，本笔记仅覆盖已生成内容。"


def now_date() -> str:
    return datetime.now().strftime("%Y%m%d")


@dataclass
class NoteResult:
    lesson_id: str
    path: Path
    text: str
    generated_by_model: bool = False
    incomplete: bool = False
    issues: list[str] = field(default_factory=list)


def lesson_id_of(lesson_dir: str | Path) -> str:
    """小节 id 以 ``lesson.json`` 为准（注意 ``L1-1`` 自身含连字符，不能按 ``-`` 截断）。"""
    lesson = Path(lesson_dir)
    data = file_repository.read_json_or_default(lesson / "lesson.json", {}) or {}
    if data.get("id"):
        return str(data["id"])
    match = re.match(r"^(L\\d+-\\d+)", lesson.name)
    return match.group(1) if match else lesson.name.split("-")[0]


def chapter_id_of(lesson_dir: str | Path) -> str:
    chapter_dir = Path(lesson_dir).parents[1]
    match = re.match(r"^(C\\d+)", chapter_dir.name)
    return match.group(1) if match else chapter_dir.name.split("-")[0]


def note_path_for(course_dir: str | Path, lesson_dir: str | Path) -> Path:
    """``notes/<章>/<小节>.md``（§3.3）。"""
    return Path(course_dir) / "notes" / chapter_id_of(lesson_dir) / f"{lesson_id_of(lesson_dir)}.md"


def assemble_notes_locally(
    *,
    lesson_id: str,
    lesson_title: str,
    objectives: list[str],
    knowledge_points: list[dict],
    blocks: list[dict],
    chat_records: list[dict],
    notebook: str = "",
    mastery: int | None = None,
    incomplete: bool = False,
) -> str:
    """不依赖模型的结构化笔记（离线可用；模型可用时由 NoteKeeper 生成同构内容）。"""
    lines = [f"# {lesson_id} {lesson_title}"]
    if incomplete:
        lines.append("")
        lines.append(INCOMPLETE_NOTICE)

    lines += ["", "## 本节目标"]
    lines += [f"- {item}" for item in objectives] or ["- （本小节未给出学习目标）"]

    lines += ["", "## 知识点提纲"]
    if knowledge_points:
        for point in knowledge_points:
            summary = str(point.get("summary") or "").strip()
            lines.append(f"- **{point.get('title', '')}**" + (f"：{summary}" if summary else ""))
    else:
        lines.append("- （本小节未列出知识点）")

    examples: list[str] = []
    misses: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "callout" and block.get("style") == "warn":
            misses.append(_plain(block.get("md", "")))
        elif kind in ("code", "table"):
            examples.append(_plain(ContentBlock.model_validate(block).to_markdown()))
        elif kind == "text" and "例如" in str(block.get("md", "")):
            examples.append(_plain(block.get("md", ""))[:400])
    lines += ["", "## 关键例子"]
    lines += [f"- {item}" for item in examples[:5]] or ["- 本节无独立例子"]

    lines += ["", "## 易错点"]
    lines += [f"- {item}" for item in misses[:5]] or ["- 本节未标注易错点"]

    lines += ["", "## 我的疑问与结论"]
    questions = [record for record in chat_records if record.get("role") == "user"]
    if questions:
        for record in questions[:5]:
            lines.append(f"- 问：{record.get('text', '')}")
        answers = [record for record in chat_records if record.get("role") == "assistant"]
        if answers:
            lines.append(f"- 结论：{str(answers[-1].get('md') or '')[:300]}")
    else:
        lines.append("暂无")
    if notebook.strip():
        lines += ["", "### 学生手写笔记", "", notebook.strip()]

    if mastery:
        lines += ["", f"（自评：{mastery} / 3）"]

    lines += ["", "## 依据与相关", "- 无外部依据"]
    return "\n".join(lines).rstrip() + "\n"


def _plain(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    return cleaned[:240]


def generate_notes(
    course_dir: str | Path,
    lesson_dir: str | Path,
    *,
    agent: object | None = None,
    incomplete: bool = False,
    outline_lesson: dict | None = None,
) -> NoteResult:
    """生成 / 重写某一节的笔记（模型可用时用 NoteKeeper，否则本地结构化）。"""
    course = Path(course_dir)
    lesson = Path(lesson_dir)
    content = lesson_service.load_lesson(lesson)
    records = lesson_service.load_chat(lesson)
    notebook_path = lesson / "notebook.md"
    notebook = file_repository.read_text_or_empty(notebook_path)

    knowledge_points = list((outline_lesson or {}).get("knowledge_points") or [])
    if not knowledge_points:
        knowledge_points = [
            {"title": str(point.get("title") or ""), "summary": str(point.get("summary") or "")}
            for point in _knowledge_points_from_outline(course, content.lesson_id)
        ]
    objectives = list((outline_lesson or {}).get("objectives") or [])

    full_blocks = list(content.blocks)
    if full_blocks and full_blocks[-1].get("type") == "done":
        full_blocks = full_blocks[:-1]

    result = NoteResult(lesson_id=content.lesson_id, path=Path(), text="", incomplete=incomplete)
    if agent is not None:
        import asyncio

        async def run() -> str:
            parts: list[str] = []
            async for piece in agent.make_notes(
                lesson_id=content.lesson_id,
                lesson_title=content.title,
                objectives=objectives,
                knowledge_points=knowledge_points,
                script_markdown=content.markdown,
                chat_records=records,
                notebook=notebook,
                mastery=content.lesson.get("mastery"),
                incomplete=incomplete,
            ):
                parts.append(piece)
            return "".join(parts)

        try:
            text = asyncio.run(run()).strip()
            if text and text.startswith("#"):
                result.text = text if text.endswith("\n") else text + "\n"
                result.generated_by_model = True
        except Exception as exc:  # noqa: BLE001 - 模型失败就退回本地结构化笔记
            result.issues.append(f"模型生成笔记失败，已用本地结构化替代：{exc}")

    if not result.text:
        result.text = assemble_notes_locally(
            lesson_id=content.lesson_id,
            lesson_title=content.title,
            objectives=objectives,
            knowledge_points=knowledge_points,
            blocks=full_blocks,
            chat_records=records,
            notebook=notebook,
            mastery=content.lesson.get("mastery"),
            incomplete=incomplete,
        )

    target = note_path_for(course, lesson)
    file_repository.write_text(target, result.text)
    result.path = target
    if incomplete:
        result.issues.append("本小节尚未学完，笔记已标注")
    return result


def _knowledge_points_from_outline(course_dir: Path, lesson_id: str) -> list[dict]:
    outline = file_repository.read_json_or_default(course_dir / "outline.json", {}) or {}
    for chapter in outline.get("chapters", []):
        for lesson in chapter.get("lessons", []):
            if lesson.get("id") == lesson_id:
                return list(lesson.get("knowledge_points") or [])
    return []


def list_notes(course_dir: str | Path) -> list[Path]:
    root = Path(course_dir) / "notes"
    return sorted(root.rglob("*.md")) if root.is_dir() else []


def read_note(path: str | Path) -> str:
    return file_repository.read_text_or_empty(path)
