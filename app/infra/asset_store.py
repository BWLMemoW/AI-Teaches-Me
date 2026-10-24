"""视觉产物的版本化落盘（《技术文档》§3.3 目录结构 / §4.7 视觉流水线、《项目文档》§8.4.1 C-08）。

约定（与既有 ``widgets/diagram-1.svg`` 完全一致）：

* 当前版本固定写 ``<stem><后缀>``，例如 ``diagram-1.svg``；
* 覆盖前先把同名旧文件改名成 ``<stem>.vK<后缀>``（K 从 1 起找第一个空位），
  因此历史版本永不丢失、可以回退；
* 多个后缀（``.svg`` + ``.png`` + ``.html``）共用同一个 K，保证同一版的产物成组；
* 文本按 UTF-8 + LF 原子写（§3.11），字节流直接写。

本模块只做落盘，不产生任何内容；调用方负责传文件名安全的 ``stem``。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..core.paths import atomic_write_text

DEFAULT_SUFFIX = ".svg"


def versioned_name(stem: str, suffix: str, version: int) -> str:
    """历史版本文件名：``diagram-1`` + ``.svg`` + ``2`` → ``diagram-1.v2.svg``。"""
    return f"{stem}.v{version}{suffix}" if version > 0 else f"{stem}{suffix}"


def next_version(directory: str | Path, stem: str, suffixes: Mapping[str, object]) -> int:
    """算出下一个可用的历史版本号（0 表示当前还没有同名文件，不需要轮转）。"""
    base = Path(directory)
    if not any((base / f"{stem}{suffix}").exists() for suffix in suffixes):
        return 0
    version = 1
    while any((base / versioned_name(stem, suffix, version)).exists() for suffix in suffixes):
        version += 1
    return version


def save_versioned(
    directory: str | Path,
    stem: str,
    payloads: Mapping[str, str | bytes],
) -> dict[str, str]:
    """把 ``payloads``（``{后缀: 内容}``）写进 ``directory``，旧的同名文件改名成 ``.vK``。

    返回 ``{后缀去点: 文件名}``，例如 ``{"svg": "diagram-1.svg", "png": "diagram-1.png"}``。
    """
    if not payloads:
        return {}
    # 后缀容错：``"svg"`` 与 ``".svg"`` 都接受（调用方两种写法都出现过）
    payloads = {
        (str(suffix) if str(suffix).startswith(".") else f".{suffix}"): payload
        for suffix, payload in payloads.items()
    }
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    suffixes = list(payloads)

    version = next_version(base, stem, payloads)
    if version:
        for suffix in suffixes:
            current = base / f"{stem}{suffix}"
            if current.exists():
                current.rename(base / versioned_name(stem, suffix, version))

    saved: dict[str, str] = {}
    for suffix, payload in payloads.items():
        target = base / f"{stem}{suffix}"
        if isinstance(payload, bytes):
            target.write_bytes(payload)
        else:
            atomic_write_text(target, str(payload))
        saved[suffix.lstrip(".")] = target.name
    return saved
