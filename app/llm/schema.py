"""结构化输出与修复重试（《项目文档》§4.4.5 / §8.2）。

总监 Agent 不走 NDJSON，而是输出**严格 JSON** 并做 Schema 校验；校验失败时把错误信息
回喂模型做**一次修复重试**，仍失败则抛 ``StructuredOutputError``。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.errors import AtmError

ModelT = TypeVar("ModelT", bound=BaseModel)

FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class StructuredOutputError(AtmError):
    """模型输出无法解析成目标结构（已尝试一次修复重试）。"""

    user_message = "模型没有按要求输出结构化内容，请重试或换一个模型"


def extract_json(text: str) -> Any:
    """从模型输出里尽力取出 JSON（容忍 ``` 围栏、前后寒暄、尾随逗号）。"""
    cleaned = text.strip()
    cleaned = FENCE_RE.sub("", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start_candidates = [index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0]
    if not start_candidates:
        raise StructuredOutputError("模型输出里找不到 JSON")
    start = min(start_candidates)
    for end in range(len(cleaned), start, -1):
        candidate = cleaned[start:end].rstrip().rstrip(",")
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise StructuredOutputError("模型输出的 JSON 无法解析")


def validate_model(payload: Any, model_cls: type[ModelT]) -> ModelT:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        raise StructuredOutputError(f"期望 JSON 对象，实际是 {type(payload).__name__}")
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        raise StructuredOutputError("结构化内容不符合 Schema", detail=str(exc)) from exc


def parse_or_raise(text: str, model_cls: type[ModelT]) -> ModelT:
    return validate_model(extract_json(text), model_cls)


def json_schema_of(model_cls: type[BaseModel], *, name: str = "result") -> dict[str, Any]:
    """给 OpenAI 兼容端点用的 ``response_format`` 载荷（§4.4.1）。"""
    return {"name": name, "schema": model_cls.model_json_schema(), "strict": False}


async def complete_json(
    client: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    model_cls: type[ModelT],
    task: str = "outline",
    repair_attempts: int = 1,
    **stream_kwargs: Any,
) -> ModelT:
    """调用模型并解析成 pydantic 模型；失败时把错误回喂做修复重试。"""

    async def collect(extra: Sequence[Mapping[str, Any]] = ()) -> str:
        parts: list[str] = []
        async for piece in client.stream_chat(list(messages) + list(extra), task=task, **stream_kwargs):
            parts.append(piece)
        return "".join(parts)

    text = await collect()
    last_error: Exception | None = None
    for _ in range(max(0, repair_attempts) + 1):
        try:
            return parse_or_raise(text, model_cls)
        except StructuredOutputError as exc:
            last_error = exc
            if repair_attempts <= 0:
                break
            repair_attempts -= 1
            text = await collect(
                [
                    {"role": "assistant", "content": text[:4000]},
                    {
                        "role": "user",
                        "content": "上面的输出不符合要求的 JSON Schema，错误是："
                        + str(exc.detail or exc)
                        + "。请**只输出**修正后的完整 JSON，不要任何解释、不要代码围栏。",
                    },
                ]
            )
    raise last_error if isinstance(last_error, AtmError) else StructuredOutputError("结构化输出失败")


async def collect_stream(stream: AsyncIterator[str]) -> str:
    parts: list[str] = []
    async for piece in stream:
        parts.append(piece)
    return "".join(parts)
