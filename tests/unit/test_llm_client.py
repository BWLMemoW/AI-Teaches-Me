"""统一 LLM 客户端的契约测试（《技术文档》§4.4.1、《项目文档》§13.1 与 T-04）。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.cancel import CancellationToken
from app.core.config import LLMProfile, default_config
from app.core.errors import AuthError, ConfigError, RetryableError
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.llm.client import LLMClient
from app.llm.usage import UsageTracker

MESSAGES = [{"role": "user", "content": "你好"}]


def _http_error(name: str, status: int | None = None) -> Exception:
    klass = type(name, (Exception,), {})
    exc = klass(name)
    if status is not None:
        exc.status_code = status
    return exc


class _FakeStream:
    def __init__(self, items=(), error=None, block=False):
        self._items = list(items)
        self._error = error
        self._block = block
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        if self._block:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


class _FakeCompletions:
    def __init__(self, factory):
        self._factory = factory
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._factory(len(self.calls))


class _FakeModels:
    def __init__(self, ids=None, error=None):
        self._ids = list(ids or [])
        self._error = error

    async def list(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=[SimpleNamespace(id=item) for item in self._ids])


class _FakeClient:
    def __init__(self, completions, models=None):
        self.chat = SimpleNamespace(completions=completions)
        self.models = models if models is not None else _FakeModels()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _chunk(text: str = "", usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=usage
    )


def _usage(prompt: int = 10, completion: int = 5):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def _store() -> SecretStore:
    store = SecretStore(backend=InMemoryBackend())
    store.set_secret("atm/deepseek-default", "sk-test-1234")
    return store


def _client(make_stream, *, models=None, sleep=None, usage=None):
    completions = _FakeCompletions(make_stream)
    holder: dict[str, object] = {}

    def factory(profile: LLMProfile, api_key: str):
        holder["api_key"] = api_key
        holder["profile"] = profile
        client = _FakeClient(completions, models)
        holder["client"] = client
        return client

    llm = LLMClient(
        default_config(),
        secret_store=_store(),
        usage=usage if usage is not None else UsageTracker(),
        client_factory=factory,
        sleep=sleep if sleep is not None else (lambda delay: asyncio.sleep(0)),
        env={},
    )
    return llm, completions, holder


async def _collect(llm: LLMClient, **kwargs) -> list[str]:
    return [piece async for piece in llm.stream_chat(MESSAGES, **kwargs)]


def test_stream_chat_yields_text_and_records_reported_usage() -> None:
    stream = _FakeStream([_chunk("第一段"), _chunk("第二段"), _chunk("", usage=_usage(12, 8))])
    usage = UsageTracker()
    llm, completions, holder = _client(lambda n: stream, usage=usage)

    pieces = asyncio.run(_collect(llm))

    assert pieces == ["第一段", "第二段"]
    assert holder["api_key"] == "sk-test-1234"
    assert len(completions.calls) == 1
    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["model"] == "deepseek-flash"
    # 讲义 / 追问这类 teach 任务默认关闭思考，避免长输出还没开始就被思考吃满预算
    assert completions.calls[0]["reasoning_effort"] == "none"
    assert usage.total()["completion_tokens"] == 8
    assert usage.total()["calls"] == 1


def test_outline_keeps_thinking_but_explicit_effort_wins() -> None:
    llm, completions, _ = _client(lambda n: _FakeStream([_chunk("x")]))

    asyncio.run(_collect(llm, task="outline"))
    asyncio.run(_collect(llm, task="teach", reasoning_effort="low"))

    assert "reasoning_effort" not in completions.calls[0]
    assert completions.calls[1]["reasoning_effort"] == "low"


def test_tools_and_json_schema_are_forwarded() -> None:
    llm, completions, _ = _client(lambda n: _FakeStream([_chunk("x")]))
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    schema = {"name": "outline", "schema": {"type": "object"}}

    asyncio.run(_collect(llm, tools=tools, json_schema=schema))

    call = completions.calls[0]
    assert call["tools"] == tools
    assert call["response_format"]["type"] == "json_schema"


def test_401_is_not_retried() -> None:
    attempts: list[int] = []

    def make(count: int):
        attempts.append(count)
        raise _http_error("AuthenticationError", 401)

    llm, completions, _ = _client(make)

    with pytest.raises(AuthError):
        asyncio.run(_collect(llm))

    assert attempts == [1]
    assert len(completions.calls) == 1


def test_429_retries_with_exponential_backoff_then_succeeds() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def make(count: int):
        if count < 3:
            raise _http_error("RateLimitError", 429)
        return _FakeStream([_chunk("好了")])

    llm, completions, _ = _client(make, sleep=fake_sleep)

    assert asyncio.run(_collect(llm)) == ["好了"]
    assert len(completions.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_retry_budget_exhausted_raises_retryable_error() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def make(count: int):
        raise _http_error("RateLimitError", 429)

    llm, completions, _ = _client(make, sleep=fake_sleep)

    with pytest.raises(RetryableError):
        asyncio.run(_collect(llm))

    assert len(completions.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_timeout_maps_to_retryable_error() -> None:
    def make(count: int):
        raise _http_error("APITimeoutError")

    llm, completions, _ = _client(make)

    with pytest.raises(RetryableError):
        asyncio.run(_collect(llm))

    assert len(completions.calls) == 3


def test_partial_output_is_not_retried() -> None:
    def make(count: int):
        return _FakeStream([_chunk("半句")], error=_http_error("RateLimitError", 429))

    llm, completions, _ = _client(make)
    pieces: list[str] = []

    async def run() -> None:
        async for piece in llm.stream_chat(MESSAGES):
            pieces.append(piece)

    with pytest.raises(RetryableError):
        asyncio.run(run())

    assert pieces == ["半句"]
    assert len(completions.calls) == 1


def test_cancel_before_start_does_not_open_client() -> None:
    token = CancellationToken()
    token.cancel()
    llm, completions, holder = _client(lambda n: _FakeStream([_chunk("不该出现")]))

    assert asyncio.run(_collect(llm, cancel=token)) == []
    assert completions.calls == []
    assert "client" not in holder


def test_cancel_mid_stream_stops_promptly_and_closes_stream() -> None:
    stream = _FakeStream([_chunk("第一句")], block=True)
    llm, _, _ = _client(lambda n: stream)
    token = CancellationToken()

    async def run() -> tuple[list[str], float]:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, token.cancel)
        started = time.perf_counter()
        pieces = [piece async for piece in llm.stream_chat(MESSAGES, cancel=token)]
        return pieces, time.perf_counter() - started

    pieces, elapsed = asyncio.run(run())

    assert pieces == ["第一句"]
    assert elapsed < 2.0
    assert stream.closed is True


def test_usage_recorded_when_endpoint_reports_nothing() -> None:
    usage = UsageTracker()
    llm, _, _ = _client(lambda n: _FakeStream([_chunk("x")]), usage=usage)

    asyncio.run(_collect(llm))

    assert usage.total() == {
        "calls": 1,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_probe_connection_lists_models_without_chat_call() -> None:
    models = _FakeModels(["deepseek-chat", "deepseek-flash"])
    llm, completions, _ = _client(lambda n: _FakeStream(), models=models)

    report = asyncio.run(llm.probe_connection())

    assert report.ok is True
    assert report.models == ["deepseek-chat", "deepseek-flash"]
    assert report.elapsed_ms >= 0
    assert completions.calls == []


def test_probe_connection_reports_auth_failure() -> None:
    models = _FakeModels(error=_http_error("AuthenticationError", 401))
    llm, _, _ = _client(lambda n: _FakeStream(), models=models)

    report = asyncio.run(llm.probe_connection())

    assert report.ok is False
    assert report.status == 401
    assert "API Key" in report.message


def test_probe_connection_tolerates_missing_models_api() -> None:
    models = _FakeModels(error=_http_error("NotFoundError", 404))
    llm, _, _ = _client(lambda n: _FakeStream(), models=models)

    report = asyncio.run(llm.probe_connection())

    assert report.ok is True
    assert "模型列表" in report.message


def test_probe_connection_requires_api_key() -> None:
    llm = LLMClient(
        default_config(),
        secret_store=SecretStore(backend=InMemoryBackend()),
        client_factory=lambda profile, key: _FakeClient(_FakeCompletions(lambda n: _FakeStream())),
        env={},
    )

    report = asyncio.run(llm.probe_connection())

    assert report.ok is False
    assert "API Key" in report.message


def test_unknown_profile_name_raises_config_error() -> None:
    llm, _, _ = _client(lambda n: _FakeStream())

    with pytest.raises(ConfigError):
        asyncio.run(_collect(llm, profile="ghost"))

def test_4xx_downgrades_json_schema_to_json_object() -> None:
    """回归：400 的降级重试曾被注释吞掉（整段成了死代码），让大纲生成必然失败。

    真实场景：DeepSeek 这类 OpenAI 兼容端点只接受 ``json_object``，收到
    ``json_schema`` 形态的 response_format 会直接 400。
    """

    def make(count: int):
        if count == 1:
            raise _http_error("BadRequestError", 400)
        return _FakeStream([_chunk("{}")])

    llm, completions, _ = _client(make)
    pieces = asyncio.run(_collect(llm, json_schema={"name": "outline", "schema": {}}))

    assert pieces == ["{}"]
    assert len(completions.calls) == 2
    assert completions.calls[0]["response_format"]["type"] == "json_schema"
    assert completions.calls[1]["response_format"] == {"type": "json_object"}


def test_4xx_ladder_then_drops_response_format_and_keeps_tools() -> None:
    def make(count: int):
        if count <= 2:
            raise _http_error("BadRequestError", 400)
        return _FakeStream([_chunk("ok")])

    llm, completions, _ = _client(make)
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    pieces = asyncio.run(_collect(llm, json_schema={"name": "o", "schema": {}}, tools=tools))

    assert pieces == ["ok"]
    assert len(completions.calls) == 3
    assert completions.calls[1]["response_format"] == {"type": "json_object"}
    assert "response_format" not in completions.calls[2]
    assert completions.calls[2]["tools"] == tools


def test_4xx_ladder_finally_drops_tools_too() -> None:
    def make(count: int):
        if count <= 3:
            raise _http_error("BadRequestError", 400)
        return _FakeStream([_chunk("ok")])

    llm, completions, _ = _client(make)
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    pieces = asyncio.run(_collect(llm, json_schema={"name": "o", "schema": {}}, tools=tools))

    assert pieces == ["ok"]
    assert len(completions.calls) == 4
    assert "tools" not in completions.calls[3]
    assert "response_format" not in completions.calls[3]


def test_4xx_ladder_is_bounded_and_then_raises() -> None:
    from app.core.errors import FatalError

    def make(count: int):
        raise _http_error("BadRequestError", 400)

    llm, completions, _ = _client(make)
    with pytest.raises(FatalError):
        asyncio.run(_collect(llm, json_schema={"name": "o", "schema": {}}))
    # json_schema → json_object → 无 response_format，共 3 次，不再无限重试
    assert len(completions.calls) == 3

def _chunk_fr(text: str = "", *, finish: str | None = None, reasoning: str | None = None):
    """带 finish_reason / reasoning_content 的流片段（推理型模型的真实形态）。"""
    delta = SimpleNamespace(content=text, reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None
    )


def test_empty_output_escalates_max_tokens() -> None:
    """回归：推理模型把 4096 预算全花在思考上 → 正文为空 → 以前只报「JSON 无法解析」。

    现在应当自动把预算翻倍重来（一个字都没吐出来，重试不会重复文本）。
    """

    def make(count: int):
        if count == 1:
            return _FakeStream([_chunk_fr("", finish="length", reasoning="想了很久")])
        return _FakeStream([_chunk("完整内容")])

    llm, completions, _ = _client(make)
    pieces = asyncio.run(_collect(llm))

    assert pieces == ["完整内容"]
    assert len(completions.calls) == 2
    first = completions.calls[0]["max_tokens"]
    assert completions.calls[1]["max_tokens"] == min(first * 2, 32768)


def test_truncated_after_text_is_reported_not_retried() -> None:
    """已经吐了半截再被截断：不整段重试（否则用户会看到重复文本），直接报明确错误。"""
    from app.core.errors import TruncatedOutput

    def make(count: int):
        return _FakeStream([_chunk("半截 JSON"), _chunk_fr("", finish="length")])

    llm, completions, _ = _client(make)
    with pytest.raises(TruncatedOutput) as info:
        asyncio.run(_collect(llm))

    assert len(completions.calls) == 1
    assert "max_tokens" in str(info.value)


def test_reasoning_only_output_is_bounded_and_raises() -> None:
    """只有思考、始终没有正文：预算最多翻倍两次，然后明确报错。"""
    from app.core.errors import TruncatedOutput

    def make(count: int):
        return _FakeStream([_chunk_fr("", reasoning="只思考不回答")])

    llm, completions, _ = _client(make)
    with pytest.raises(TruncatedOutput):
        asyncio.run(_collect(llm))

    assert len(completions.calls) == 2  # 第一次 + 一次扩预算
    assert completions.calls[1]["max_tokens"] > completions.calls[0]["max_tokens"]

def test_max_tokens_override_is_forwarded() -> None:
    llm, completions, _ = _client(lambda n: _FakeStream([_chunk("x")]))
    asyncio.run(_collect(llm, max_tokens=32768))
    assert completions.calls[0]["max_tokens"] == 32768

def test_tool_only_round_is_not_truncation() -> None:
    """回归：只发起工具调用、没有正文的一轮是正常的（Tool Loop 会回喂），
    以前会被误判成「只思考没正文」而直接失败。"""

    def tool_chunk():
        call = SimpleNamespace(
            index=0, id="call_1", function=SimpleNamespace(name="get_progress", arguments="{}")
        )
        delta = SimpleNamespace(content=None, reasoning_content="先看看进度", tool_calls=[call])
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason="tool_calls")], usage=None
        )

    def make(count: int):
        return _FakeStream([tool_chunk()])

    llm, completions, _ = _client(make)
    sink: list[dict] = []

    async def run() -> list[str]:
        return [piece async for piece in llm.stream_chat(MESSAGES, tools=[{"x": 1}], tool_sink=sink)]

    # 该轮没有正文，也不该报错；工具调用交给上层循环继续
    assert asyncio.run(run()) == []
    assert len(completions.calls) == 1  # 没有被当成截断而重试
