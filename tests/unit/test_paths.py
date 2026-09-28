"""路径安全的单元测试（《技术文档》§3.11、《项目文档》T-01 / T-02 与 §13.6）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import InvalidName, PathEscape
from app.core.paths import (
    MAX_COMPONENT_LENGTH,
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    cleanup_stale_tmp,
    read_json,
    read_text,
    safe_component,
    safe_join,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("线性代数速通", "线性代数速通"),
        (" 矩阵 ", "矩阵"),
        ("name. ", "name"),
        ("AI Teaches Me 使用指南", "AI Teaches Me 使用指南"),
        ('含:冒号<和>引号"', "含冒号和引号"),
    ],
)
def test_safe_component_cleans_valid_names(raw: str, expected: str) -> None:
    assert safe_component(raw) == expected


def test_safe_component_truncates_long_names() -> None:
    assert len(safe_component("长" * 100)) == MAX_COMPONENT_LENGTH


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "../../evil", "..\\evil", "..", "C:\\Windows\\x", "a/b", "CON", "con.txt", "NUL.md", "lpt1"],
)
def test_safe_component_rejects_illegal_names(bad: str) -> None:
    with pytest.raises(InvalidName):
        safe_component(bad)


def test_safe_component_rejects_non_string() -> None:
    with pytest.raises(InvalidName):
        safe_component(123)  # type: ignore[arg-type]


def test_safe_join_builds_nested_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = safe_join(root, "Courses", "线性代数速通", "course.json")
    assert target == (root / "Courses" / "线性代数速通" / "course.json").resolve()
    assert str(target).startswith(str(root.resolve()))


def test_safe_join_without_parts_returns_root(tmp_path: Path) -> None:
    assert safe_join(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize("bad", ["..", "../evil", "a/../../b", "C:\\Windows", "", "   "])
def test_safe_join_rejects_escape(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PathEscape):
        safe_join(tmp_path, bad)


def test_atomic_write_text_writes_lf_utf8_and_leaves_no_tmp(tmp_path: Path) -> None:
    path = atomic_write_text(tmp_path / "a.md", "第一行\n第二行\n")
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert read_text(path).startswith("第一行")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    atomic_write_text(path, "一")
    atomic_write_text(path, "二")
    assert read_text(path) == "二"


def test_json_roundtrip_keeps_chinese(tmp_path: Path) -> None:
    path = atomic_write_json(tmp_path / "data" / "config.json", {"name": "樱花主题", "n": 1})
    assert read_json(path) == {"name": "樱花主题", "n": 1}
    assert read_json(tmp_path / "missing.json", default="x") == "x"


def test_append_jsonl_appends_flushed_lines(tmp_path: Path) -> None:
    path = tmp_path / "chat.jsonl"
    append_jsonl(path, {"role": "user", "text": "为什么？"})
    append_jsonl(path, {"role": "assistant", "blocks": 1})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "为什么？"


def test_cleanup_stale_tmp_removes_only_tmp(tmp_path: Path) -> None:
    (tmp_path / "a.json.tmp").write_text("{}", encoding="utf-8")
    nested = tmp_path / "data" / "cache"
    nested.mkdir(parents=True)
    (nested / "b.tmp").write_text("x", encoding="utf-8")
    keep = tmp_path / "keep.json"
    keep.write_text("{}", encoding="utf-8")

    removed = cleanup_stale_tmp(tmp_path)

    assert len(removed) == 2
    assert not (tmp_path / "a.json.tmp").exists()
    assert not (nested / "b.tmp").exists()
    assert keep.exists()
