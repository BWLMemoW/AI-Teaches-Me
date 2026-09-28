"""NDJSON 流式解析的单元测试（《技术文档》§4.4.4、《项目文档》T-03）。"""

from __future__ import annotations

import pytest

from app.llm.stream import (
    MAX_CONSECUTIVE_FAILURES,
    BlockStreamParser,
    iter_ndjson,
    parse_blocks,
)

LINE_META = '{"type":"meta","lesson_id":"L1-2","depth":"standard","version":1}'
LINE_TEXT = '{"type":"text","md":"## 为什么重量和质量总被搞混？"}'
LINE_DONE = '{"type":"done","tokens":1533}'
SAMPLE = LINE_META + "\n" + LINE_TEXT + "\n" + LINE_DONE + "\n"


def test_parse_full_sample_and_protocol_ok() -> None:
    blocks, parser = parse_blocks(SAMPLE)

    assert [block["type"] for block in blocks] == ["meta", "text", "done"]
    assert parser.protocol_broken is False
    assert parser.broken_lines == []
    assert parser.validate_protocol() == []


def test_chunked_feed_emits_only_complete_lines() -> None:
    parser = BlockStreamParser()
    partial = LINE_TEXT[:12]

    first = parser.feed(LINE_META + "\n" + partial)
    assert [block["type"] for block in first] == ["meta"]
    assert parser.pending == partial

    rest = parser.feed(LINE_TEXT[12:] + "\n" + LINE_DONE + "\n")
    assert [block["type"] for block in rest] == ["text", "done"]
    assert parser.pending == ""


def test_blank_lines_and_fences_are_ignored() -> None:
    text = "```jsonl\n" + LINE_META + "\n\n" + LINE_DONE + "\n```\n"
    blocks, parser = parse_blocks(text)

    assert [block["type"] for block in blocks] == ["meta", "done"]
    assert parser.broken_lines == []
    assert parser.consecutive_failures == 0


def test_trailing_comma_is_tolerated() -> None:
    blocks, parser = parse_blocks(LINE_META + ",\n" + LINE_DONE + ",\n")
    assert [block["type"] for block in blocks] == ["meta", "done"]
    assert parser.broken_lines == []


def test_bad_line_is_skipped_and_later_lines_continue() -> None:
    text = LINE_META + "\n这不是 JSON\n" + LINE_DONE + "\n"
    blocks, parser = parse_blocks(text)

    assert [block["type"] for block in blocks] == ["meta", "done"]
    assert parser.broken_lines == ["这不是 JSON"]
    assert parser.protocol_broken is False


def test_five_consecutive_failures_trigger_markdown_fallback() -> None:
    parser = BlockStreamParser()
    bad = "\n".join(f"坏行 {i}" for i in range(MAX_CONSECUTIVE_FAILURES)) + "\n"

    parser.feed(LINE_META + "\n")
    parser.feed(bad)

    assert parser.protocol_broken is True
    assert len(parser.broken_lines) == MAX_CONSECUTIVE_FAILURES
    assert parser.feed(LINE_DONE + "\n") == []
    assert parser.finish() == []
    assert parser.fallback_markdown() == LINE_META + "\n" + bad + LINE_DONE + "\n"
    assert LINE_META in parser.fallback_markdown()


def test_failures_reset_after_a_good_line() -> None:
    text = (
        "坏1\n坏2\n坏3\n坏4\n"
        + LINE_META
        + "\n坏5\n坏6\n坏7\n坏8\n"
    )
    blocks, parser = parse_blocks(text)

    assert [block["type"] for block in blocks] == ["meta"]
    assert parser.protocol_broken is False
    assert len(parser.broken_lines) == 8


def test_non_dict_json_counts_as_failure() -> None:
    blocks, parser = parse_blocks("[1,2,3]\n" + LINE_META + "\n")
    assert [block["type"] for block in blocks] == ["meta"]
    assert parser.broken_lines == ["[1,2,3]"]


def test_bom_and_unknown_type_are_handled() -> None:
    blocks, parser = parse_blocks('\ufeff{"type":"future_block","x":1}\n')
    assert blocks == [{"type": "future_block", "x": 1}]
    assert parser.broken_lines == []


def test_validate_protocol_reports_missing_meta_and_done() -> None:
    blocks, parser = parse_blocks(LINE_TEXT + "\n")
    assert blocks
    issues = parser.validate_protocol()
    assert len(issues) == 2
    assert "meta" in issues[0] and "done" in issues[1]


def test_finish_flushes_last_line_without_newline() -> None:
    parser = BlockStreamParser()
    assert parser.feed(LINE_META) == []
    assert [block["type"] for block in parser.finish()] == ["meta"]


def test_iter_ndjson_skips_broken_lines() -> None:
    lines = [LINE_META, "", "坏行", "[1,2]", LINE_DONE]
    assert [block["type"] for block in iter_ndjson(lines)] == ["meta", "done"]


def test_first_and_last_block_properties() -> None:
    _, parser = parse_blocks(SAMPLE)
    assert parser.first_block is not None and parser.first_block["type"] == "meta"
    assert parser.last_block is not None and parser.last_block["type"] == "done"
    assert BlockStreamParser().first_block is None
