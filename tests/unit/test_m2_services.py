"""M2 服务层测试：人设、工具执行器、讲义生成、覆盖率、版本链、追问、进度与三级三态。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.persona import Persona, PersonaFormatError, dump_persona, load_persona_file, parse_persona
from app.agents.teacher import TeacherAgent
from app.agents.tools import ToolSpec, build_registry
from app.bootstrap import bootstrap_workspace
from app.core.errors import AtmError
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.tool_executor import CallBudget, ToolExecutor, summarize_log
from app.services import lesson_service
from app.services.persona_service import PersonaService
from app.services.progress_service import ProgressService, resolve_assessment


def _lesson_dir(tmp_path: Path) -> Path:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    return tmp_path / "Courses" / "ATM 使用指南" / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


# --- 人设 -----------------------------------------------------------------
def test_persona_files_parse_and_dump_roundtrip() -> None:
    persona = load_persona_file(Path("prompts/personas/catgirl-assistant.md"))

    assert persona.id == "catgirl-assistant"
    assert persona.name == "猫娘助教"
    assert persona.allow_roleplay is True
    assert "search_materials" in persona.tools
    assert persona.version == 1

    again = parse_persona(dump_persona(persona))
    assert again.id == persona.id
    assert again.tools == persona.tools


def test_persona_parse_error_reports_line_number() -> None:
    with pytest.raises(PersonaFormatError) as excinfo:
        parse_persona("---\nid: Bad_ID\nname: x\n---\nbody", source="bad.md")
    assert excinfo.value.line == 2
    assert "bad.md" in str(excinfo.value)


def test_persona_service_priority_and_roleplay_flag(tmp_path: Path) -> None:
    service = PersonaService(user_dir=tmp_path / "personas")

    strict = service.resolve(global_default="strict-professor")
    assert strict.id == "strict-professor"

    catgirl = service.resolve(global_default="strict-professor", course_persona="catgirl-assistant")
    assert catgirl.id == "catgirl-assistant"
    assert "主人" in catgirl.body

    temporary = service.resolve(global_default="strict-professor", temporary="default-teacher")
    assert temporary.id == "default-teacher"

    no_roleplay = Persona(id="x", name="x", body="你是猫，自称本喵\n讲课要认真。", allow_roleplay=False)
    assert "自称本喵" not in no_roleplay.effective_body
    assert "讲课要认真" in no_roleplay.effective_body


def test_persona_import_export_and_validation(tmp_path: Path) -> None:
    service = PersonaService(user_dir=tmp_path / "personas")
    issues = service.validate_text("这不是人设文件")
    assert issues and "第 1 行" in issues[0]

    source = tmp_path / "incoming.md"
    source.write_text(dump_persona(service.get("punk-coach")), encoding="utf-8")
    imported = service.import_persona(source)

    assert imported.id == "punk-coach"
    assert (tmp_path / "personas" / "punk-coach.md").exists()

    exported = service.export_persona("catgirl-assistant", tmp_path / "out" / "cat.md")
    assert "catgirl-assistant" in exported.read_text(encoding="utf-8")


# --- 工具与执行器 ---------------------------------------------------------
def test_tool_executor_caches_and_reports(tmp_path: Path) -> None:
    calls: list[str] = []
    registry = build_registry()
    registry.register(
        ToolSpec(
            "demo_search",
            "演示用可缓存工具",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            lambda query: calls.append(query) or {"hits": [query]},
            cacheable=True,
        )
    )
    executor = ToolExecutor(registry)

    first = asyncio.run(executor.run({"name": "demo_search", "arguments": json.dumps({"query": "矩阵"})}))
    second = asyncio.run(executor.run({"name": "demo_search", "arguments": json.dumps({"query": "矩阵"})}))

    assert first["ok"] is True and second["cached"] is True
    assert calls == ["矩阵"]
    assert summarize_log(executor.log)["calls"] == 2


def test_tool_executor_structured_errors() -> None:
    registry = build_registry()
    executor = ToolExecutor(registry)

    unknown = asyncio.run(executor.run({"name": "nope", "arguments": "{}"}))
    bad_args = asyncio.run(executor.run({"name": "read_outline", "arguments": "not json"}))
    missing = asyncio.run(executor.run({"name": "get_lesson_kps", "arguments": "{}"}))

    assert unknown["error"] == "unknown_tool"
    assert bad_args["error"] == "bad_arguments"
    assert missing["error"] == "bad_arguments" and "lesson_id" in missing["message"]


def test_tool_executor_timeout_and_budget() -> None:
    import time as _time

    registry = build_registry()
    registry.register(
        ToolSpec("slow", "慢工具", {"type": "object", "properties": {}}, lambda: _time.sleep(0.5), timeout=0.1)
    )
    executor = ToolExecutor(registry, budget=CallBudget(daily_limit=1, per_lesson_limits={"slow": 5}))

    first = asyncio.run(executor.run({"name": "slow", "arguments": "{}"}))
    second = asyncio.run(executor.run({"name": "read_outline", "arguments": "{}"}))

    assert first["error"] == "timeout"
    assert second["error"] == "budget_exceeded"


def test_tool_executor_parallel_limit() -> None:
    registry = build_registry()
    executor = ToolExecutor(registry)
    calls = [{"name": "read_outline", "arguments": "{}"} for _ in range(5)]

    results = asyncio.run(executor.run_many(calls))

    assert sum(1 for item in results if item["ok"]) == 3
    assert sum(1 for item in results if item.get("error") == "too_many_calls") == 2


def test_build_registry_tools_can_read_outline_and_progress(tmp_path: Path) -> None:
    outline = {"chapters": [{"id": "C1", "lessons": [{"id": "L1-1", "knowledge_points": [{"id": "kp1", "title": "点"}]}]}]}
    registry = build_registry(outline=outline, course_dir=tmp_path, progress={"total_minutes": 3})

    executor = ToolExecutor(registry)
    kps = asyncio.run(executor.run({"name": "get_lesson_kps", "arguments": json.dumps({"lesson_id": "L1-1"})}))
    progress = asyncio.run(executor.run({"name": "get_progress", "arguments": "{}"}))

    assert kps["result"][0]["id"] == "kp1"
    assert progress["result"]["total_minutes"] == 3


# --- 讲义生成 -------------------------------------------------------------
class _FakeClient:
    """假模型：按脚本吐出 NDJSON 文本（分片流式）。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def stream_chat(self, messages, **kwargs):
        text = self._responses.pop(0) if self._responses else ""
        self.prompts.append("\n".join(str(item.get("content", "")) for item in messages))
        for index in range(0, len(text), 29):
            yield text[index : index + 29]


def _ndjson(blocks: list[dict]) -> str:
    return "\n".join(json.dumps(block, ensure_ascii=False) for block in blocks) + "\n"


def test_generate_script_writes_blocks_incrementally_and_bumps_version(tmp_path: Path) -> None:
    lesson_dir = _lesson_dir(tmp_path)
    context = {
        "lesson": {"id": "L1-1", "title": "先配置你的 AI", "objectives": ["能配置"]},
        "chapter": {"id": "C1", "title": "三分钟上手", "summary": ""},
        "course": {"name": "ATM 使用指南", "profile": {"target_audience": "新手"}},
        "knowledge_points": [{"id": "kp-seed-001", "title": "工作区与课程数据分离", "must_master": True}],
        "depth": "minimal",
    }
    blocks = [
        {"type": "meta", "lesson_id": "L1-1", "depth": "minimal", "version": 1},
        {"type": "text", "md": "## 工作区与课程数据分离\n\nCourses/ 是你的课程资产。"},
        {"type": "done", "tokens": 0},
    ]
    teacher = TeacherAgent(_FakeClient([_ndjson(blocks)]))

    outcome = asyncio.run(
        lesson_service.generate_script(lesson_dir, teacher=teacher, context=context, depth="minimal")
    )

    assert [block["type"] for block in outcome.blocks] == ["meta", "text", "done"]
    assert outcome.coverage_ok is True
    assert outcome.protocol_broken is False

    lesson = json.loads((lesson_dir / "lesson.json").read_text(encoding="utf-8"))
    assert lesson["current_version"] == 2  # 种子讲义是 v1，生成后自增
    assert lesson["status"] == "in_progress"
    assert "工作区与课程数据分离" in (lesson_dir / "script.md").read_text(encoding="utf-8")
    assert len(lesson_service.load_chat(lesson_dir)) == 0


def test_generate_script_runs_coverage_round_for_missing_kp(tmp_path: Path) -> None:
    lesson_dir = _lesson_dir(tmp_path)
    context = {
        "lesson": {"id": "L1-1", "title": "t"},
        "chapter": {"id": "C1"},
        "course": {"name": "c"},
        "knowledge_points": [
            {"id": "kp-a", "title": "工作区分离"},
            {"id": "kp-b", "title": "密钥存储进凭据库"},
        ],
    }
    first = [
        {"type": "meta", "lesson_id": "L1-1", "version": 1},
        {"type": "text", "md": "只讲了工作区分离。"},
        {"type": "done", "tokens": 0},
    ]
    second = [
        {"type": "meta", "lesson_id": "L1-1", "version": 1},
        {"type": "text", "md": "补充：密钥存储进凭据库，不落明文。"},
        {"type": "done", "tokens": 0},
    ]
    client = _FakeClient([_ndjson(first), _ndjson(second)])
    teacher = TeacherAgent(client)

    outcome = asyncio.run(lesson_service.generate_script(lesson_dir, teacher=teacher, context=context))

    assert outcome.coverage_ok is True
    assert len(client.prompts) == 2
    assert "漏讲" in client.prompts[1]


def test_regenerate_archives_previous_version(tmp_path: Path) -> None:
    lesson_dir = _lesson_dir(tmp_path)
    context = {"lesson": {"id": "L1-1", "title": "t"}, "chapter": {}, "course": {}, "knowledge_points": []}
    blocks = [{"type": "meta", "lesson_id": "L1-1"}, {"type": "text", "md": "第一版"}, {"type": "done", "tokens": 0}]
    teacher = TeacherAgent(_FakeClient([_ndjson(blocks), _ndjson(blocks)]))

    asyncio.run(lesson_service.generate_script(lesson_dir, teacher=teacher, context=context))
    asyncio.run(
        lesson_service.generate_script(
            lesson_dir, teacher=teacher, context=context, revision_reason="换种讲法"
        )
    )

    history = sorted((lesson_dir / "history").glob("v*.md"))
    assert history, "重讲前应把旧版本归档到 history/"
    lesson = json.loads((lesson_dir / "lesson.json").read_text(encoding="utf-8"))
    assert lesson["current_version"] == 3
    assert lesson["versions"][-1]["reason"] == "换种讲法"


def test_ask_question_appends_chat_jsonl(tmp_path: Path) -> None:
    lesson_dir = _lesson_dir(tmp_path)
    context = {"lesson": {"id": "L1-1", "title": "t"}, "chapter": {}, "course": {}, "knowledge_points": []}
    answer = [
        {"type": "text", "md": "因为要保证两边变换能接上。"},
        {"type": "done", "tokens": 0},
    ]
    teacher = TeacherAgent(_FakeClient([_ndjson(answer)]))

    async def run() -> list[str]:
        return [
            piece
            async for piece in lesson_service.ask_question(
                lesson_dir, teacher=teacher, question="为什么行数等于列数才能相乘？", context=context, selection="矩阵 A 为 m×n"
            )
        ]

    pieces = asyncio.run(run())
    records = lesson_service.load_chat(lesson_dir)

    assert "".join(pieces)
    assert records[0]["role"] == "user"
    assert records[0]["selection"] == "矩阵 A 为 m×n"
    assert records[-1]["role"] == "assistant"
    assert records[-1]["blocks"] >= 1


# --- 进度与三级三态 -------------------------------------------------------
def test_progress_marks_mastery_and_schedules_review(tmp_path: Path) -> None:
    lesson_dir = _lesson_dir(tmp_path)
    service = ProgressService(lesson_dir.parents[3])

    service.update_lesson(lesson_dir, status=lesson_service.STATUS_COMPLETED, mastery=1, minutes=6)

    progress = service.load()
    assert progress.lessons["L1-1"].status == "completed"
    assert progress.lessons["L1-1"].minutes == 6
    assert [item.lesson_id for item in progress.review_queue] == ["L1-1"]
    assert progress.review_queue[0].interval_days == 1

    service.schedule_review("L1-1", 1)
    assert service.load().review_queue[0].interval_days == 3  # 间隔递增（简化版 SM-2）

    service.exclude_from_review("L1-1")
    assert service.load().review_queue[0].excluded is True
    assert service.due_reviews() == []


def test_progress_stats_and_assessment_resolution(tmp_path: Path) -> None:
    lesson_dir = _lesson_dir(tmp_path)
    service = ProgressService(lesson_dir.parents[3])
    service.update_lesson(lesson_dir, status=lesson_service.STATUS_COMPLETED, mastery=3, minutes=4)

    stats = service.stats()
    assert stats.completed_lessons == 1
    assert stats.total_minutes == 4
    assert stats.average_mastery == 3

    assert resolve_assessment(
        lesson_quiz="inherit", lesson_self="inherit", course_quiz="off", course_self="inherit",
        global_quiz=True, global_self=True,
    ) == {"quiz": False, "self_assessment": True}
    assert resolve_assessment(
        lesson_quiz="on", lesson_self="inherit", course_quiz="off", course_self="off",
        global_quiz=False, global_self=False,
    ) == {"quiz": True, "self_assessment": False}
