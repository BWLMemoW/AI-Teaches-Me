"""M19 测试：Tool Loop 回喂给模型的 tool_calls 必须是标准结构（否则 DeepSeek 422）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.base import AgentBase, normalize_tool_calls


def test_normalize_tool_calls_matches_openai_shape() -> None:
    normalized = normalize_tool_calls(
        [
            {"id": "call_1", "name": "get_progress", "arguments": "{}"},
            {"id": "", "name": "read_outline", "arguments": {"scope": "all"}},
        ]
    )

    assert normalized[0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_progress", "arguments": "{}"},
    }
    assert normalized[1]["type"] == "function"
    assert normalized[1]["function"]["name"] == "read_outline"
    assert json.loads(normalized[1]["function"]["arguments"]) == {"scope": "all"}
    assert normalized[1]["id"].startswith("call_")


class _FakeClient:
    """第一次返回工具调用，第二次返回正文；并记录每轮收到的 messages。"""

    def __init__(self) -> None:
        self.rounds: list[list[dict]] = []
        self.calls = 0

    async def stream_chat(self, messages, **kwargs):
        self.rounds.append([dict(item) for item in messages])
        self.calls += 1
        if self.calls == 1:
            sink = kwargs.get("tool_sink")
            if sink is not None:
                sink.append({"id": "call_1", "name": "get_progress", "arguments": "{}"})
            return
        yield "已经写完了。"


class _Executor:
    async def run(self, call, cancel=None):
        return {"calls": 1}


def test_tool_loop_sends_standard_tool_calls_back() -> None:
    client = _FakeClient()
    agent = AgentBase(client)

    async def run() -> list[str]:
        return [
            piece
            async for piece in agent.run_tool_loop(
                [{"role": "user", "content": "讲一节"}], [], executor=_Executor(), system="sys"
            )
        ]

    pieces = asyncio.run(run())

    assert pieces == ["已经写完了。"]
    assert client.calls == 2
    second_round = client.rounds[1]
    assistant = next(item for item in second_round if item.get("tool_calls"))
    call = assistant["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_progress"
    assert call["function"]["arguments"] == "{}"
    tool_message = next(item for item in second_round if item.get("role") == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    assert "<untrusted" in tool_message["content"]
