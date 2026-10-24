"""小节内容服务（《技术文档》§4.3；M0 读取预置讲义，M2 扩展生成与落盘）。

服务层是事务边界：读讲义 + 更新 ``lesson.json`` / ``progress.json`` 都在这里完成，
UI 只拿结果。本模块**不导入 Qt**。
"""

from __future__ import annotations

import json
import base64
import mimetypes
import secrets

from dataclasses import dataclass, field
from datetime import datetime
import shutil
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..core.paths import append_jsonl, atomic_write_json, atomic_write_text, read_json, read_text
from ..llm.stream import BlockStreamParser, iter_ndjson, parse_blocks

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


SETTINGS_VERSION = 2


def migrate_legacy_lesson_settings(course_dir: str | Path) -> int:
    """把旧版自动写入的 ``standard`` / ``default-teacher`` 改为「跟随设置」。

    老版本建课时无论全局偏好如何，都会固定写这两个值；结果就是每切一个小节，
    老师与深度看起来都要重设。这里只迁移**旧版默认值**：用户手动选过的
    ``minimal`` / ``deep`` 或其它人设一律保留。迁移只做一次，之后通过
    ``settings_version`` 标记跳过。
    """
    root = Path(course_dir)
    changed = 0
    for path in sorted(root.glob("chapters/*/lessons/*/lesson.json")):
        lesson = read_json(path, default={}) or {}
        if int(lesson.get("settings_version") or 0) >= SETTINGS_VERSION:
            continue
        migrated = False
        if str(lesson.get("depth") or "") == "standard":
            lesson["depth"] = "inherit"
            migrated = True
        persona_id = str((lesson.get("persona") or {}).get("id") or "")
        if persona_id in ("", "default-teacher"):
            lesson["persona"] = {"id": "inherit", "version": 1}
            migrated = True
        lesson["settings_version"] = SETTINGS_VERSION
        atomic_write_json(path, lesson)
        if migrated:
            changed += 1
    return changed


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

def iter_lesson_dirs(course_dir: str | Path) -> list[Path]:
    """课程下所有小节目录（``chapters/*/lessons/*``）。"""
    root = Path(course_dir) / "chapters"
    if not root.is_dir():
        return []
    found: list[Path] = []
    for chapter in sorted(path for path in root.iterdir() if path.is_dir()):
        lessons = chapter / "lessons"
        if lessons.is_dir():
            found.extend(sorted(path for path in lessons.iterdir() if path.is_dir()))
    return found


def set_lesson_status(lesson_dir: str | Path, status: str, *, mastery: int | None = None) -> dict:
    """直接改小节状态（批量标记用，FR-04 的「标记完成 / 取消完成」）。"""
    directory = Path(lesson_dir)
    lesson = read_json(directory / "lesson.json", default={}) or {}
    if not lesson:
        raise FileNotFoundError(f"缺少 lesson.json：{directory}")
    lesson["status"] = status
    lesson["completed_at"] = (lesson.get("completed_at") or now_iso()) if status == STATUS_COMPLETED else None
    if mastery is not None:
        lesson["mastery"] = mastery
    atomic_write_json(directory / "lesson.json", lesson)
    return lesson


def sync_course_progress(course_dir: str | Path) -> dict:
    """把课程下所有小节状态汇总进 ``progress.json``（保留已有 review_queue）。"""
    root = Path(course_dir)
    progress_path = root / "progress.json"
    progress = read_json(progress_path, default={}) or {}
    lessons: dict[str, dict] = {}

    for lesson_dir in iter_lesson_dirs(root):
        lesson = read_json(lesson_dir / "lesson.json", default={}) or {}
        lesson_id = str(lesson.get("id") or lesson_dir.name.split("-")[0])
        lessons[lesson_id] = {
            "status": lesson.get("status", STATUS_NOT_STARTED),
            "mastery": lesson.get("mastery"),
            "quiz_score": lesson.get("quiz_score"),
            "minutes": int((lesson.get("time_spent_seconds") or 0) // 60),
        }

    progress["schema_version"] = int(progress.get("schema_version") or 1)
    progress["lessons"] = lessons
    progress["total_minutes"] = sum(int(item.get("minutes") or 0) for item in lessons.values())
    progress["completed_lessons"] = sum(1 for item in lessons.values() if item.get("status") == STATUS_COMPLETED)
    progress["updated_at"] = now_iso()
    progress.setdefault("review_queue", [])
    atomic_write_json(progress_path, progress)
    return progress
# --- 讲义生成（§4.3 / §4.4 流式 + §3.11 逐块增量落盘） --------------------
@dataclass
class GenerationOutcome:
    blocks: list[dict]
    missing_knowledge_points: list[str] = field(default_factory=list)
    reference_missing: list[str] = field(default_factory=list)
    protocol_broken: bool = False
    fallback_markdown: str = ""
    tokens: int = 0

    @property
    def coverage_ok(self) -> bool:
        return not self.missing_knowledge_points


def render_script_markdown(lesson: dict, blocks: Sequence[dict]) -> str:
    """由内容块生成人读版 ``script.md``（§3.4：md 与 blocks 同源）。"""
    title = str(lesson.get("title") or "")
    header = f"# {title}\n\n" if title else ""
    return header + blocks_to_markdown(list(blocks))


def append_block(lesson_dir: str | Path, block: dict) -> None:
    """每完成一个块就追加到 ``script.blocks.jsonl`` 并重写 ``script.md``（§3.11）。"""
    directory = Path(lesson_dir)
    append_jsonl(directory / "script.blocks.jsonl", block)
    lesson = read_json(directory / "lesson.json", default={}) or {}
    blocks = [json.loads(line) for line in (read_text(directory / "script.blocks.jsonl")).splitlines() if line.strip()]
    atomic_write_text(directory / "script.md", render_script_markdown(lesson, blocks))


# 讲义是长文（五段式 + 图示 + 例子），实测 16384 会被截断；这里给足预算，
# 追问等短任务仍用档案自己的 max_tokens。
LESSON_MAX_TOKENS = 32768
#: 课件覆盖补讲的上限：缺得太多说明是「整份课件没讲」这种结构问题，
#: 硬塞一轮补讲只会产出更长的废话，不如如实报给用户。
REFERENCE_COVERAGE_MAX_ITEMS = 12


async def generate_script(
    lesson_dir: str | Path,
    *,
    teacher: Any,
    context: Mapping[str, Any],
    persona_body: str | None = None,
    persona_label: str | None = None,
    depth: str = "standard",
    cancel: Any = None,
    executor: Any = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    lesson_id: str | None = None,
    course_id: str | None = None,
    model: str | None = None,
    revision_reason: str = "",
    coverage_rounds: int = 1,
    reference_coverage_rounds: int = 1,
    on_block: Callable[[dict], None] | None = None,
    on_partial: Callable[[str], None] | None = None,
) -> GenerationOutcome:
    """生成讲义：流式解析 → 逐块落盘 → 覆盖率自检（必要时补全一轮）→ 版本自增。"""
    from ..agents.teacher import TeacherAgent

    directory = Path(lesson_dir)
    directory.mkdir(parents=True, exist_ok=True)
    blocks_path = directory / "script.blocks.jsonl"
    if revision_reason:
        _archive_current_version(directory)
    atomic_write_text(blocks_path, "")

    parser = BlockStreamParser()
    blocks: list[dict] = []

    async def consume(**extra: Any) -> None:
        async for piece in teacher.teach(
            {**dict(context), **extra},
            cancel=cancel,
            executor=executor,
            tools=tools,
            max_tokens=LESSON_MAX_TOKENS,
            persona_body=persona_body,
            persona_label=persona_label,
            depth=depth,
            model=model,
            lesson_id=lesson_id,
            course_id=course_id,
        ):
            if on_partial is not None:
                partial = parser.partial_text()
                if partial:
                    on_partial(partial)
            for block in parser.feed(piece):
                blocks.append(block)
                append_block(directory, block)
                if on_block is not None:
                    on_block(block)
        for block in parser.finish():
            blocks.append(block)
            append_block(directory, block)

    await consume()

    outcome = GenerationOutcome(blocks=blocks, protocol_broken=parser.protocol_broken)
    knowledge_points = list(context.get("knowledge_points") or [])

    if parser.protocol_broken:
        outcome.fallback_markdown = parser.fallback_markdown()
        atomic_write_text(
            directory / "script.md",
            "# （协议错乱，已按整段 Markdown 渲染）\n\n" + outcome.fallback_markdown,
        )
    elif knowledge_points:
        missing = TeacherAgent.coverage_issues(blocks, knowledge_points)
        while missing and coverage_rounds > 0:
            coverage_rounds -= 1
            titles = "、".join(str(point.get("title") or point.get("id")) for point in knowledge_points if str(point.get("id")) in missing or str(point.get("title")) in missing)
            parser = BlockStreamParser()
            await consume(
                coverage_note=f"上一轮漏讲了这些知识点：{titles}。请只补充这几点的讲解（NDJSON，继续用同一协议），不要重复已讲内容。"
            )
            blocks.extend(parser.feed(""))
            missing = TeacherAgent.coverage_issues(blocks, knowledge_points)
        outcome.missing_knowledge_points = missing

    # --- 课件覆盖自检（C）：知识点讲全了，还要看跟课件对不对得上 --------
    if not outcome.protocol_broken:
        digest = list(context.get("reference_digest") or [])
        if digest:
            missing_refs = TeacherAgent.reference_coverage_issues(blocks, digest)
            if (
                missing_refs
                and reference_coverage_rounds > 0
                and len(missing_refs) <= REFERENCE_COVERAGE_MAX_ITEMS
            ):
                reference_coverage_rounds -= 1
                titles = "、".join(missing_refs)
                parser = BlockStreamParser()
                await consume(
                    coverage_note=(
                        f"上一轮讲义漏掉了这些课件内容：{titles}。请只补充讲解这些部分"
                        "（NDJSON，继续用同一协议；需要原话可用 read_material_slice 取该页原文），"
                        "不要重复已讲内容。"
                    )
                )
                blocks.extend(parser.feed(""))
                missing_refs = TeacherAgent.reference_coverage_issues(blocks, digest)
            outcome.reference_missing = missing_refs

    stored_depth = str((read_json(directory / "lesson.json", default={}) or {}).get("depth") or "")
    # 「跟随设置」要一直跟着全局走；生成结束后不能把它固化成当时的档位。
    _bump_version(
        directory,
        model=model or "",
        reason=revision_reason or "首次生成",
        depth=stored_depth if stored_depth == "inherit" else depth,
    )
    outcome.tokens = sum(len(str(block)) for block in blocks) // 4  # 粗略估计：端点未回报真实 token
    atomic_write_text(directory / "script.md", render_script_markdown(read_json(directory / "lesson.json", default={}) or {}, blocks))
    return outcome


def _archive_current_version(lesson_dir: Path) -> None:
    """重讲前把当前版本归档到 ``history/``（§3.3、AC-22：旧版本不丢）。"""
    script = lesson_dir / "script.md"
    blocks = lesson_dir / "script.blocks.jsonl"
    if not script.exists() and not blocks.exists():
        return
    lesson = read_json(lesson_dir / "lesson.json", default={}) or {}
    version = int(lesson.get("current_version") or 1)
    history = lesson_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    if script.exists():
        shutil.copy2(script, history / f"v{version}.md")
    if blocks.exists():
        shutil.copy2(blocks, history / f"v{version}.blocks.jsonl")


def _bump_version(lesson_dir: Path, *, model: str, reason: str, depth: str) -> dict:
    from ..core.models import LessonVersion
    from ..infra import file_repository

    path = lesson_dir / "lesson.json"
    lesson = read_json(path, default={}) or {}
    if not lesson:
        raise FileNotFoundError(f"缺少 lesson.json：{lesson_dir}")
    current = int(lesson.get("current_version") or 0)
    next_version = current + 1
    versions = list(lesson.get("versions") or [])
    versions.append(LessonVersion(version=next_version, model=model, reason=reason).model_dump(mode="json"))
    lesson.update(
        {
            "current_version": next_version,
            "versions": versions,
            "depth": depth,
            "status": STATUS_IN_PROGRESS,
            "started_at": lesson.get("started_at") or now_iso(),
        }
    )
    file_repository.write_json(path, lesson)
    return lesson


# --- 追问（§3.6 chat.jsonl 追加写；M27 起支持「一课多会话」） --------------
CHAT_FILE_NAME = "chat.jsonl"
SESSION_KIND = "session"
LEGACY_SESSION_ID = "chat-legacy"
DEFAULT_SESSION_TITLE = "新会话"


def new_chat_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"chat-{stamp}-{secrets.token_hex(2)}"


def _chat_records(lesson_dir: str | Path) -> list[dict]:
    """读原始行（含会话标记行）。坏行跳过，避免一行脏数据毁掉整段记录。"""
    path = Path(lesson_dir) / CHAT_FILE_NAME
    if not path.exists():
        return []
    records: list[dict] = []
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def chat_sessions(lesson_dir: str | Path) -> list[dict[str, Any]]:
    """本小节的会话列表（按出现顺序，后出现的同名会话覆盖标题）。

    * 会话行：``{"kind":"session","id":…,"title":…,"created_at":…}``
    * 删除标记：``{"kind":"session","id":…,"deleted":true}``（追加写，不物理删）
    * 老版本只有消息行的 ``chat.jsonl`` → 合成一个「历史追问」会话，保证不丢记录；
      这个虚拟会话的归属判定与 :func:`load_chat` 保持一致：**没有 session 字段的老记录**与
      **显式写成 ``chat-legacy`` 的新记录**都算它（M35 修：读写不对称会让新回答落盘后读不回来）
    """
    sessions: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    legacy_messages = 0
    for record in _chat_records(lesson_dir):
        if record.get("kind") == SESSION_KIND and record.get("id"):
            session_id = str(record["id"])
            if record.get("deleted"):
                sessions.pop(session_id, None)
                if session_id in order:
                    order.remove(session_id)
                continue
            if session_id not in sessions:
                order.append(session_id)
            sessions[session_id] = {
                "id": session_id,
                "title": str(record.get("title") or DEFAULT_SESSION_TITLE),
                "created_at": str(record.get("created_at") or ""),
            }
            continue
        if record.get("role"):
            record_session = str(record.get("session") or "")
            if not record_session or record_session == LEGACY_SESSION_ID:
                legacy_messages += 1
    if legacy_messages and LEGACY_SESSION_ID not in sessions:
        order.insert(0, LEGACY_SESSION_ID)
        sessions[LEGACY_SESSION_ID] = {
            "id": LEGACY_SESSION_ID,
            "title": "历史追问",
            "created_at": "",
        }
    return [sessions[session_id] for session_id in order]


def create_chat_session(
    lesson_dir: str | Path, *, title: str = DEFAULT_SESSION_TITLE, session_id: str | None = None
) -> dict[str, Any]:
    """新建一个追问会话（追加一行会话记录）。"""
    directory = Path(lesson_dir)
    directory.mkdir(parents=True, exist_ok=True)
    session = {
        "id": session_id or new_chat_session_id(),
        "title": title or DEFAULT_SESSION_TITLE,
        "created_at": now_iso(),
    }
    append_jsonl(directory / CHAT_FILE_NAME, {"kind": SESSION_KIND, **session})
    return session


def rename_chat_session(lesson_dir: str | Path, session_id: str, title: str) -> None:
    """改名同样是追加写：后出现的同名会话记录覆盖标题。"""
    create_chat_session(lesson_dir, title=title, session_id=session_id)


def delete_chat_session(lesson_dir: str | Path, session_id: str) -> None:
    """删除 = 追加一条删除标记（chat.jsonl 保持只追加，不物理改写）。"""
    append_jsonl(
        Path(lesson_dir) / CHAT_FILE_NAME,
        {"kind": SESSION_KIND, "id": str(session_id), "deleted": True, "ts": now_iso()},
    )


def latest_chat_session_id(lesson_dir: str | Path) -> str:
    sessions = chat_sessions(lesson_dir)
    return sessions[-1]["id"] if sessions else ""


def ensure_chat_session(lesson_dir: str | Path) -> dict[str, Any]:
    """取最近一个会话；一个都没有就建一个（打开小节即可直接问）。"""
    sessions = chat_sessions(lesson_dir)
    if sessions:
        return sessions[-1]
    return create_chat_session(lesson_dir)


def append_chat_turn(lesson_dir: str | Path, record: dict, *, session_id: str | None = None) -> Path:
    directory = Path(lesson_dir)
    payload = {"ts": now_iso(), **record}
    if session_id:
        payload.setdefault("session", session_id)
    path = append_jsonl(directory / CHAT_FILE_NAME, payload)
    # 第一条提问自动给会话起个名字（仍用追加写，覆盖同一 id 的标题）
    if session_id and record.get("role") == "user":
        sessions = {item["id"]: item for item in chat_sessions(directory)}
        current = sessions.get(session_id)
        if current is not None and current["title"] == DEFAULT_SESSION_TITLE:
            text = str(record.get("text") or "").strip().replace("\n", " ")
            if text:
                rename_chat_session(directory, session_id, text[:18] + ("…" if len(text) > 18 else ""))
    return path


def load_chat(lesson_dir: str | Path, *, session_id: str | None = None) -> list[dict]:
    """读追问消息（不含会话标记行）。``session_id=None`` 时返回所有会话的消息。"""
    records = _chat_records(lesson_dir)
    if session_id and session_id != LEGACY_SESSION_ID:
        removed = {
            str(item.get("id"))
            for item in records
            if item.get("kind") == SESSION_KIND and item.get("deleted") and item.get("id")
        }
        if session_id in removed:
            return []  # 已删除的会话：界面不再展示（原始记录仍留在文件里）
    messages: list[dict] = []
    for record in records:
        if record.get("kind") == SESSION_KIND or not record.get("role"):
            continue
        if session_id is None:
            messages.append(record)
            continue
        record_session = str(record.get("session") or "")
        if session_id == LEGACY_SESSION_ID:
            # 老记录没有 session 字段；新记录可能显式写成 ``chat-legacy``——两者都算这个会话。
            # 只认前者的话，「历史追问」里新写的回答一落盘就再读不回来（界面表现为「答案消失」）。
            if not record_session or record_session == LEGACY_SESSION_ID:
                messages.append(record)
        elif record_session == session_id:
            messages.append(record)
    return messages


def build_ask_refs(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把本小节的知识点与资料片段整理成「引用卡片」，作为答疑的依据。"""
    refs: list[dict[str, Any]] = []
    for point in context.get("knowledge_points") or []:
        refs.append(
            {
                "type": "outline",
                "title": f"{point.get('id', '')} {point.get('title', '')}".strip(),
                "excerpt": str(point.get("summary") or ""),
            }
        )
    for snippet in context.get("material_snippets") or []:
        refs.append({"type": "material_page", "title": "资料片段", "excerpt": str(snippet)[:400]})
    return refs


def ask_outline_summary(context: Mapping[str, Any]) -> str:
    lesson = context.get("lesson") or {}
    chapter = context.get("chapter") or {}
    course = context.get("course") or {}
    return (
        f"{course.get('name', '')} / {chapter.get('id', '')} {chapter.get('title', '')}"
        f" / {lesson.get('id', '')} {lesson.get('title', '')}"
    )


async def ask_question(
    lesson_dir: str | Path,
    *,
    teacher: Any,
    question: str,
    context: Mapping[str, Any],
    selection: str = "",
    persona_body: str | None = None,
    persona_label: str | None = None,
    depth: str = "standard",
    cancel: Any = None,
    executor: Any = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    lesson_id: str | None = None,
    course_id: str | None = None,
    qa: Any = None,
    persona_name: str | None = None,
    session_id: str | None = None,
    attachments: Sequence[Mapping[str, Any]] = (),
) -> AsyncIterator[str]:
    """小节内追问：携带小节上下文与选中片段，回答流式产出并追加写 ``chat.jsonl``。

    **必须走答疑 Agent（``qa``）**：教师 Agent 的提示词是「讲完本小节」，
    以前追问复用它、还把问题塞进一个它根本不读的字段，结果模型只会把讲义
    再讲一遍（真实 bug）。没有 qa 时退回教师的补丁版提示词（会读 ``ask``）。
    """
    directory = Path(lesson_dir)
    session_id = session_id or ensure_chat_session(directory)["id"]
    # 历史答疑（含模型回答）只取**本会话**、且在写入本次提问**之前**取，供承接语气
    history = [
        record
        for record in load_chat(directory, session_id=session_id)
        if record.get("text") or record.get("md")
    ]
    append_chat_turn(
        directory,
        {"role": "user", "text": question, "selection": selection or None},
        session_id=session_id,
    )
    collected: list[str] = []
    if qa is not None:
        stream_iter = qa.answer(
            question=question,
            selection=selection,
            refs=build_ask_refs(context),
            outline_summary=ask_outline_summary(context),
            history=history,
            persona_name=persona_name,
            persona_body=persona_body,
            persona_label=persona_label,
            depth=depth,
            cancel=cancel,
            executor=executor,
            tools=tools,
            lesson_id=lesson_id,
            course_id=course_id,
            attachments=attachments,
        )
    else:
        prompt = f"学生的追问：{question}\n"
        if selection:
            prompt += f"他选中的讲义片段：{selection}\n"
        for item in attachments:
            prompt += f"（他引用了讲义里的一张图：{str(item.get('caption') or item.get('path') or '')}）\n"
        prompt += (
            "只回答这个问题，不要复述讲义、不要重讲本节；"
            "请用 NDJSON 回答（先 text 块给结论，最后 done 块）。"
        )
        stream_iter = teacher.teach(
            {**dict(context), "ask": prompt},
            cancel=cancel,
            executor=executor,
            tools=tools,
            persona_body=persona_body,
            persona_label=persona_label,
            depth=depth,
            lesson_id=lesson_id,
            course_id=course_id,
        )
    async for piece in stream_iter:
        collected.append(piece)
        yield piece
    text = "".join(collected)
    blocks, _ = parse_blocks(text)
    append_chat_turn(
        directory,
        {"role": "assistant", "blocks": len(blocks), "tokens": len(text) // 4, "md": text},
        session_id=session_id,
    )


# --- 讲义标注（划词标注 → 引用到追问） -------------------------------------
# 存储约定：每个小节一个 ``annotations.json``（人可读、可手工编辑），
# 结构 ``{"version": 1, "items": [...]}``。标注只记录**原文片段 + 备注 + 定位提示**，
# 不修改 script.blocks.jsonl / script.md，所以重讲、换深度都不会破坏标注。
ANNOTATIONS_FILE_NAME = "annotations.json"
ANNOTATION_COLORS = ("yellow", "green", "blue", "pink")
DEFAULT_ANNOTATION_COLOR = "yellow"
MAX_ANNOTATION_QUOTE = 600
MAX_ANNOTATION_NOTE = 2000


def new_annotation_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"an-{stamp}-{secrets.token_hex(2)}"


def _annotations_path(lesson_dir: str | Path) -> Path:
    return Path(lesson_dir) / ANNOTATIONS_FILE_NAME


def _clean_color(color: str | None) -> str:
    value = str(color or "").strip().lower()
    return value if value in ANNOTATION_COLORS else DEFAULT_ANNOTATION_COLOR


def load_annotations(lesson_dir: str | Path) -> list[dict[str, Any]]:
    """读本小节的标注（按加入顺序）。文件损坏时返回空列表，不影响阅读讲义。"""
    try:
        store = read_json(_annotations_path(lesson_dir), default={}) or {}
    except ValueError:  # 手改坏了 / 写到一半断电：标注丢了可以，讲义不能打不开
        return []
    items = store.get("items") if isinstance(store, dict) else None
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        cleaned.append(
            {
                "id": str(item.get("id") or new_annotation_id()),
                "ts": str(item.get("ts") or ""),
                "quote": quote,
                "note": str(item.get("note") or ""),
                "color": _clean_color(item.get("color")),
                "block_index": item.get("block_index"),
                "block_type": str(item.get("block_type") or ""),
            }
        )
    return cleaned


def save_annotations(lesson_dir: str | Path, items: Sequence[Mapping[str, Any]]) -> Path:
    """整表原子写回（标注数量是「人划的」，不会大到需要增量写）。"""
    payload = {"version": 1, "items": [dict(item) for item in items]}
    return atomic_write_json(_annotations_path(lesson_dir), payload)


def add_annotation(
    lesson_dir: str | Path,
    *,
    quote: str,
    note: str = "",
    color: str = DEFAULT_ANNOTATION_COLOR,
    block_index: int | None = None,
    block_type: str = "",
) -> dict[str, Any]:
    """新增一条标注（同一片段重复标注时合并，避免列表里出现一堆一模一样的条目）。"""
    text = " ".join(str(quote or "").split()).strip()[:MAX_ANNOTATION_QUOTE]
    if not text:
        raise ValueError("标注内容不能为空")
    items = load_annotations(lesson_dir)
    for item in items:
        if item["quote"] == text:
            if note:
                item["note"] = str(note).strip()[:MAX_ANNOTATION_NOTE]
                save_annotations(lesson_dir, items)
            return item
    record = {
        "id": new_annotation_id(),
        "ts": now_iso(),
        "quote": text,
        "note": str(note or "").strip()[:MAX_ANNOTATION_NOTE],
        "color": _clean_color(color),
        "block_index": block_index,
        "block_type": str(block_type or ""),
    }
    items.append(record)
    save_annotations(lesson_dir, items)
    return record


def update_annotation(
    lesson_dir: str | Path,
    annotation_id: str,
    *,
    note: str | None = None,
    color: str | None = None,
) -> dict[str, Any] | None:
    items = load_annotations(lesson_dir)
    for item in items:
        if item["id"] != str(annotation_id):
            continue
        if note is not None:
            item["note"] = str(note).strip()[:MAX_ANNOTATION_NOTE]
        if color is not None:
            item["color"] = _clean_color(color)
        save_annotations(lesson_dir, items)
        return item
    return None


def delete_annotation(lesson_dir: str | Path, annotation_id: str) -> bool:
    items = load_annotations(lesson_dir)
    remaining = [item for item in items if item["id"] != str(annotation_id)]
    if len(remaining) == len(items):
        return False
    save_annotations(lesson_dir, remaining)
    return True


def clear_annotations(lesson_dir: str | Path) -> int:
    items = load_annotations(lesson_dir)
    save_annotations(lesson_dir, [])
    return len(items)


def annotation_citation(item: Mapping[str, Any]) -> str:
    """把一条标注变成喂给答疑 Agent 的引用文本。"""
    quote = " ".join(str(item.get("quote") or "").split())
    note = " ".join(str(item.get("note") or "").split())
    if note:
        return f"「{quote}」（我的批注：{note}）"
    return f"「{quote}」"


def annotations_markdown(lesson_dir: str | Path) -> str:
    """标注 → Markdown 清单（供「复制全部标注」/ 笔记整理使用）。"""
    lines: list[str] = []
    for item in load_annotations(lesson_dir):
        lines.append(f"- “{item['quote']}”")
        if item.get("note"):
            lines.append(f"  - 批注：{item['note']}")
    return "\n".join(lines)


# --- 引用图片到追问（把讲义里的图真的发给模型，§4.14 多模态读图） -------------
ATTACHMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024


def build_image_attachment(path: str | Path, *, caption: str = "") -> dict[str, Any] | None:
    """把本地图片打包成可随追问发送的附件（data URL）。

    只在文件真实存在、后缀是受支持图片、体积不超上限时才返回，否则返回 ``None``
    ——调用方据此退回「只引用文字」，不影响提问。
    """
    file = Path(path)
    try:
        if not file.is_file() or file.suffix.lower() not in ATTACHMENT_SUFFIXES:
            return None
        data = file.read_bytes()
    except OSError:
        return None
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        return None
    mime = mimetypes.guess_type(file.name)[0] or "image/png"
    return {
        "path": str(file),
        "caption": str(caption or "").strip(),
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }
