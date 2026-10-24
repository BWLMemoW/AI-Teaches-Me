"""路径安全与原子写（《技术文档》§3.11「一致性、原子写与版本迁移」、§1.5 红线 4、§6）。

两条硬约束（违反即返工）：

1. 一切写入路径先清洗：拒绝 ``..``、绝对路径与 Windows 保留名（对应《项目文档》T-01 / T-02）；
2. 解析出的最终路径必须落在工作区根目录之内（对应 T-01「不创建任何文件」）。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import InvalidName, PathEscape

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
MAX_COMPONENT_LENGTH = 80
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
SEPARATORS = re.compile(r"[\\/]")
TMP_SUFFIX = ".tmp"
TITLE_REPLACEMENTS = str.maketrans(
    {
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
)


def safe_component(name: str) -> str:
    """清洗单个路径片段（课程名 / 文件名）。

    与《技术文档》§3.11 示例的差异：《项目文档》T-01 要求 ``../../evil`` 抛
    ``InvalidName`` 而不是被清洗成 ``evil``，因此含路径分隔符或 ``..`` 时直接判非法。
    """
    if not isinstance(name, str):
        raise InvalidName("名称必须是字符串")
    raw = name.strip()
    if not raw:
        raise InvalidName("名称不能为空")
    if SEPARATORS.search(raw) or ".." in raw or DRIVE_PREFIX.match(raw):
        raise InvalidName(f"名称不能包含路径分隔符、.. 或盘符：{raw!r}")

    cleaned = INVALID_CHARS.sub("", raw).strip(" .")
    if not cleaned:
        raise InvalidName(f"名称清洗后为空：{name!r}")
    if cleaned.upper().split(".")[0] in WINDOWS_RESERVED:
        raise InvalidName(f"名称是 Windows 保留名：{cleaned!r}")
    return cleaned[:MAX_COMPONENT_LENGTH]


def safe_title_component(title: str, *, fallback: str = "未命名") -> str:
    """把「章 / 小节标题」变成可读的目录片段。

    模型很自然会写出 ``INNER / LEFT / RIGHT`` 这种标题；``safe_component`` 的职责是
    拒绝路径穿越，直接拿标题去调它会误伤正常内容。这里只做**展示层的可读替换**：
    半角非法字符换成全角、``..`` 换成省略号，再把结果交给 ``safe_component`` 做长度与
    保留名校验。标题本身仍原样保存在 ``outline.json`` 里，界面显示不变。
    """
    text = " ".join(str(title or "").replace("\r", " ").replace("\n", " ").split())
    text = text.translate(TITLE_REPLACEMENTS)
    # 全角标点前后不夹半角空格，目录名更像中文标题
    text = re.sub(r"\s+([：；，。！？])", r"\1", text)
    text = re.sub(r"([：；，。！？])\s+", r"\1", text)
    while ".." in text:
        text = text.replace("..", "…")
    try:
        return safe_component(text)
    except InvalidName:
        return safe_component(fallback)


def safe_join(root: str | Path, *parts: str) -> Path:
    """把 ``parts`` 拼到 ``root`` 下，并保证结果仍在 ``root`` 之内。"""
    root_path = Path(root).resolve()
    if parts:
        for part in parts:
            text = str(part)
            if not text.strip():
                raise PathEscape("路径片段不能为空")
            if SEPARATORS.search(text) or DRIVE_PREFIX.match(text) or ".." in SEPARATORS.split(text):
                raise PathEscape(f"路径片段不合法：{text!r}")
    target = root_path.joinpath(*[Path(str(part)) for part in parts]).resolve()
    if target != root_path and not str(target).startswith(str(root_path) + os.sep):
        raise PathEscape(f"路径越界：{target}")
    return target


def atomic_write_text(path: str | Path, text: str) -> Path:
    """同分区原子写（先写 .tmp 再 os.replace），编码固定 UTF-8、换行固定 LF。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + TMP_SUFFIX)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def atomic_write_json(path: str | Path, data: Any) -> Path:
    """原子写 JSON（人可读：缩进 2、不转义中文）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, text)


def read_text(path: str | Path) -> str:
    """读文本（utf-8-sig：容忍带 BOM 的手改文件）。"""
    return Path(path).read_text(encoding="utf-8-sig")


def read_json(path: str | Path, default: Any = None) -> Any:
    """读 JSON；解析失败抛 ValueError，缺文件返回 ``default``。"""
    target = Path(path)
    if not target.exists():
        return default
    return json.loads(read_text(target))


def append_jsonl(path: str | Path, record: Any) -> Path:
    """追加一行 NDJSON 并立刻 flush（《技术文档》§3.11：追加写逐行 flush）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return target


def iter_stale_tmp(root: str | Path) -> Iterator[Path]:
    """列出残留的 ``*.tmp``（上次写入中途崩溃的痕迹）。"""
    base = Path(root)
    if not base.exists():
        return
    yield from sorted(base.rglob(f"*{TMP_SUFFIX}"))


def cleanup_stale_tmp(root: str | Path) -> list[Path]:
    """启动时丢弃残留临时文件（保留原文件不动），返回被清理的路径。"""
    removed: list[Path] = []
    for path in iter_stale_tmp(root):
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed
