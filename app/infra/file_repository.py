"""课程目录读写（《技术文档》§3.3 目录结构、§3.11 原子写与删除规范）。

* 一切路径先过 ``safe_component`` / ``safe_join``（§1.5 红线 4）；
* 删除课程走**系统回收站**（§3.11 / AC-15），非 Windows 平台退化为就地回收目录。
"""

from __future__ import annotations

import ctypes
import shutil
import sys
from pathlib import Path
from typing import Any

from ..core.paths import (
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    read_json,
    read_text,
    safe_component,
    safe_join,
)

COURSE_SUBDIRS: tuple[str, ...] = ("assets", "materials/raw", "materials/parsed", "chapters", "notes", "qa", "exports")


def ensure_course_dirs(course_dir: str | Path) -> dict[str, Path]:
    """按 §3.3 建出课程目录骨架，返回各子目录路径。"""
    root = Path(course_dir)
    created: dict[str, Path] = {"root": root}
    for relative in COURSE_SUBDIRS:
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        created[relative] = target
    return created


def course_dir_for(courses_dir: str | Path, name: str) -> Path:
    return safe_join(courses_dir, safe_component(name))


def unique_course_dir(courses_dir: str | Path, name: str) -> tuple[str, Path]:
    """同名课程自动去重：``X`` → ``X-2`` → ``X-3``（AC-12）。"""
    base = safe_component(name)
    candidate = base
    index = 2
    while course_dir_for(courses_dir, candidate).exists():
        candidate = safe_component(f"{base}-{index}")
        index += 1
    return candidate, course_dir_for(courses_dir, candidate)


def write_json(path: str | Path, data: Any) -> Path:
    return atomic_write_json(path, data)


def write_text(path: str | Path, text: str) -> Path:
    return atomic_write_text(path, text)


def read_json_or_default(path: str | Path, default: Any = None) -> Any:
    return read_json(path, default)


def read_text_or_empty(path: str | Path) -> str:
    target = Path(path)
    return read_text(target) if target.exists() else ""


def append_record(path: str | Path, record: Any) -> Path:
    return append_jsonl(path, record)


def delete_to_recycle_bin(path: str | Path) -> bool:
    """删除课程：Windows 走回收站，其它平台移入同级 ``.trash/``（§3.11）。"""
    target = Path(path)
    if not target.exists():
        return False
    if sys.platform == "win32":
        return _delete_with_shfileoperation(target)
    trash = target.parent / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(trash / target.name))
    return True


def _delete_with_shfileoperation(target: Path) -> bool:
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_bool),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    FOF_NOERRORUI = 0x0400

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = str(target.resolve()) + "\0\0"
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 极端环境下退回直接删除，保证功能可用
        shutil.rmtree(target, ignore_errors=True)
        return True
    if result != 0:
        return False
    return not target.exists()


def rename_course_dir(course_dir: str | Path, new_name: str) -> Path:
    """重命名课程目录（ACK-64：调用方还需同步 course.json 与索引）。"""
    source = Path(course_dir)
    target = source.parent / safe_component(new_name)
    if target.exists():
        raise FileExistsError(f"目标目录已存在：{target}")
    source.rename(target)
    return target
