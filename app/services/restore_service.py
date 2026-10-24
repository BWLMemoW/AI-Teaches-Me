"""从备份 zip 恢复课程（与 backup_service 对应，含 zip-slip 防护）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from ..core.paths import safe_component, safe_join


def _restore_part(part: str) -> str:
    """保留课程库自己的隐藏元数据目录，其余片段仍走路径清洗。"""
    if part == ".library":
        return ".library"
    return safe_component(part)


def restore_from_zip(zip_path: str | Path, target_root: str | Path) -> list[Path]:
    """把备份解包到目标目录；每个成员都过 ``safe_join``，拒绝目录穿越。"""
    archive_path = Path(zip_path)
    root = Path(target_root)
    root.mkdir(parents=True, exist_ok=True)
    restored: list[Path] = []

    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            parts = [
                _restore_part(part)
                for part in Path(member.filename).parts
                if part not in ("", ".", "..")
            ]
            if not parts:
                continue
            destination = safe_join(root, *parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                destination.write_bytes(source.read())
            restored.append(destination)
    return restored
