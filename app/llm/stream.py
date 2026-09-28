"""NDJSON 内容块流式解析（《技术文档》§4.4.4、《项目文档》§13.2）。

容错策略是硬要求：

1. 整行 JSON 解析失败 → 记录并跳过，继续消费后续块；
2. **连续 5 行失败** → 判定协议错乱，回退「整段文本按 Markdown 渲染」；
3. 围栏行（``\u0060\u0060\u0060jsonl`` / ``\u0060\u0060\u0060``）与空行不计入失败；
4. 未知 ``type`` 不在解析层拦截，交给渲染层降级（保证老版本客户端不崩）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

MAX_CONSECUTIVE_FAILURES = 5
FENCE_LINES = frozenset({"```", "```jsonl", "```json", "```ndjson"})
PROTOCOL_FIRST_TYPE = "meta"
PROTOCOL_LAST_TYPE = "done"


class BlockStreamParser:
    """把模型输出的 token 流切成一个个完整的内容块。"""

    def __init__(self, *, max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES) -> None:
        self._buffer = ""
        self._raw_parts: list[str] = []
        self._max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0
        self.protocol_broken = False
        self.broken_lines: list[str] = []
        self.blocks: list[dict[str, Any]] = []

    # --- 只读状态 -------------------------------------------------------
    @property
    def pending(self) -> str:
        """尚未消费的缓冲（不含最终块）。"""
        return self._buffer

    @property
    def raw_text(self) -> str:
        """累计收到的原文（供协议错乱时整段回退渲染）。"""
        return "".join(self._raw_parts)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def first_block(self) -> dict[str, Any] | None:
        return self.blocks[0] if self.blocks else None

    @property
    def last_block(self) -> dict[str, Any] | None:
        return self.blocks[-1] if self.blocks else None

    # --- 主流程 ---------------------------------------------------------
    def feed(self, text: str) -> list[dict[str, Any]]:
        """喂入一段流式文本，返回本次新解析出的完整内容块。"""
        if not text:
            return []
        self._raw_parts.append(text)
        if self.protocol_broken:
            return []

        self._buffer += text
        emitted: list[dict[str, Any]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            block = self._consume_line(line)
            if block is not None:
                emitted.append(block)
            if self.protocol_broken:
                break
        return emitted

    def finish(self) -> list[dict[str, Any]]:
        """流结束：把最后一行（无换行结尾）也消费掉。"""
        if self.protocol_broken:
            return []
        emitted: list[dict[str, Any]] = []
        if self._buffer:
            line, self._buffer = self._buffer, ""
            block = self._consume_line(line)
            if block is not None:
                emitted.append(block)
        return emitted

    def fallback_markdown(self) -> str:
        """协议错乱时的整段回退文本。"""
        return self.raw_text

    def validate_protocol(self) -> list[str]:
        """协议自检：首行必须是 ``meta``，最后一行必须是 ``done``。"""
        issues: list[str] = []
        if not self.blocks:
            return issues
        if self.blocks[0].get("type") != PROTOCOL_FIRST_TYPE:
            issues.append(f"首个内容块不是 {PROTOCOL_FIRST_TYPE}：{self.blocks[0].get('type')!r}")
        if self.blocks[-1].get("type") != PROTOCOL_LAST_TYPE:
            issues.append(f"最后一个内容块不是 {PROTOCOL_LAST_TYPE}：{self.blocks[-1].get('type')!r}")
        return issues

    # --- 内部 -----------------------------------------------------------
    def _consume_line(self, line: str) -> dict[str, Any] | None:
        text = line.strip().lstrip("\ufeff")
        if not text or text in FENCE_LINES:
            return None
        candidate = text[:-1].rstrip() if text.endswith(",") else text
        try:
            block = json.loads(candidate)
        except json.JSONDecodeError:
            self._record_failure(text)
            return None
        if not isinstance(block, dict):
            self._record_failure(text)
            return None
        self._consecutive_failures = 0
        self.blocks.append(block)
        return block

    def _record_failure(self, text: str) -> None:
        self.broken_lines.append(text)
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_consecutive_failures:
            self.protocol_broken = True


def parse_blocks(text: str, **kwargs: Any) -> tuple[list[dict[str, Any]], BlockStreamParser]:
    """一次性解析（非流式场景与测试用），返回（块列表，解析器）。"""
    parser = BlockStreamParser(**kwargs)
    blocks = parser.feed(text)
    blocks.extend(parser.finish())
    return blocks, parser


def iter_ndjson(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """逐行解析已在磁盘上的 NDJSON（坏行直接跳过）。"""
    for line in lines:
        text = line.strip()
        if not text or text in FENCE_LINES:
            continue
        try:
            block = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(block, dict):
            yield block
