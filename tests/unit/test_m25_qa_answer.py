"""M25 测试：课内追问必须**回答问题**，而不是复述讲义。

真实 bug：追问复用了教师 Agent，而教师的用户提示词根本不读 `ask` 字段，
学生的问题压根没送进模型，模型只看到「请讲完本小节」→ 把讲义又讲了一遍。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agents.qa import QaAgent
from app.agents.teacher import TeacherAgent
from app.services import lesson_service

QUESTION = "可以画一下这节课的思维导图吗"
CONTEXT = {
    "course": {"name": "游戏系统策划", "goal": "求职入行"},
    "chapter": {"id": "C1", "title": "入门"},
    "lesson": {"id": "L1-1", "title": "系统策划是谁：岗位角色与团队协作"},
    "knowledge_points": [{"id": "kp-001", "title": "职责边界", "summary": "体验 → 规则"}],
    "material_snippets": ["策划是把体验翻译成规则的人。"],
    "depth": "deep",
}


class _RecordingClient:
    """记录模型收到的 messages，并按 NDJSON 吐回一段回答。"""

    def __init__(self, pieces: list[str] | None = None) -> None:
        self.pieces = pieces or ['{"type":"text","md":"这是答案"}\n', '{"type":"done","tokens":0}\n']
        self.rounds: list[list[dict]] = []

    async def stream_chat(self, messages, **kwargs):
        self.rounds.append([dict(item) for item in messages])
        for piece in self.pieces:
            yield piece


def test_teacher_prompt_with_ask_contains_the_question() -> None:
    """兜底路径：老师提示词的 ask 分支必须把问题带上，且不再要求「讲完本小节」。"""
    agent = TeacherAgent(object())
    prompt = agent.build_user_prompt({**CONTEXT, "ask": f"学生的追问：{QUESTION}"})

    assert QUESTION in prompt
    assert "不要逐条复述" in prompt
    assert "请按输出协议，用 NDJSON 讲完本小节" not in prompt


def test_qa_prompt_forbids_repeating_the_lecture() -> None:
    agent = QaAgent(object())
    system = agent.system_prompt(depth="deep")
    prompt = agent.build_user_prompt(question=QUESTION, outline_summary="游戏系统策划 / C1 / L1-1")

    assert "不要复述讲义" in system
    assert "五段式" not in system  # 答疑不再用讲义骨架
    assert "diagram" in system  # 要求画图时必须真的画
    assert "diagram" in prompt
    assert QUESTION in prompt


def test_ask_question_sends_the_question_to_the_model(tmp_path: Path) -> None:
    """核心回归：学生的问题必须出现在发给模型的 messages 里。"""
    lesson_dir = tmp_path / "L1-1"
    lesson_dir.mkdir()
    client = _RecordingClient()
    qa = QaAgent(client)

    async def run() -> list[str]:
        return [
            piece
            async for piece in lesson_service.ask_question(
                lesson_dir, teacher=None, qa=qa, question=QUESTION, context=CONTEXT
            )
        ]

    pieces = asyncio.run(run())

    assert "这是答案" in "".join(pieces)
    system = client.rounds[0][0]["content"]
    user = client.rounds[0][1]["content"]
    assert QUESTION in user
    assert "不要复述讲义" in system
    assert "kp-001" in user  # 依据卡片带上了本节知识点
    records = lesson_service.load_chat(lesson_dir)
    assert records[0]["role"] == "user" and records[0]["text"] == QUESTION
    assert records[-1]["role"] == "assistant"


def test_ask_question_falls_back_to_teacher_with_question(tmp_path: Path) -> None:
    """没有 qa 时退回教师，但问题仍然要进提示词（老路径也不能再复述讲义）。"""
    lesson_dir = tmp_path / "L1-2"
    lesson_dir.mkdir()
    client = _RecordingClient()
    teacher = TeacherAgent(client)

    async def run() -> list[str]:
        return [
            piece
            async for piece in lesson_service.ask_question(
                lesson_dir, teacher=teacher, question=QUESTION, context=CONTEXT
            )
        ]

    asyncio.run(run())

    user = client.rounds[0][1]["content"]
    assert QUESTION in user
    assert "不要复述讲义" in user


def test_ask_question_passes_history_of_previous_turns(tmp_path: Path) -> None:
    lesson_dir = tmp_path / "L1-3"
    lesson_dir.mkdir()
    lesson_service.append_chat_turn(lesson_dir, {"role": "user", "text": "上一问"})
    lesson_service.append_chat_turn(lesson_dir, {"role": "assistant", "md": "上一答"})

    client = _RecordingClient()
    qa = QaAgent(client)

    async def run() -> None:
        async for _ in lesson_service.ask_question(
            lesson_dir, teacher=None, qa=qa, question="接着问", context=CONTEXT
        ):
            pass

    asyncio.run(run())

    user = client.rounds[0][1]["content"]
    assert "上一问" in user and "上一答" in user
    assert "接着问" in user