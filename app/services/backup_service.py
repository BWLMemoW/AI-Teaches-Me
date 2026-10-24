"""备份服务（《项目文档》FR-13 / §3.11：整库或单课程备份为 zip，**不含密钥**）。"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

EXCLUDED_DIRS = ("data", "logs", ".library", "__pycache__")
EXCLUDED_FILES = ("secrets.bin",)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d")


def _zip_tree(root: Path, archive: zipfile.ZipFile, *, arc_prefix: str = "") -> int:
    count = 0
    for path in sorted(root.rglob("*")):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_dir() or path.name in EXCLUDED_FILES or path.suffix == ".tmp":
            continue
        archive.write(path, arcname=str(Path(arc_prefix) / path.relative_to(root)))
        count += 1
    return count


def backup_course(course_dir: str | Path, target: str | Path | None = None) -> Path:
    """备份单门课程（默认写到课程自己的 ``exports/``）。"""
    course = Path(course_dir)
    destination = (
        Path(target)
        if target is not None
        else course / "exports" / f"{course.name}-备份-{_stamp()}.zip"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        _zip_tree(course, archive, arc_prefix=course.name)
    return destination


def backup_library(courses_dir: str | Path, target: str | Path | None = None) -> Path:
    """备份整个课程库（不含 ``data/`` 里的密钥与索引）。"""
    root = Path(courses_dir)
    destination = (
        Path(target)
        if target is not None
        else root.parent / "exports" / f"课程库备份-{_stamp()}.zip"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        folders_file = root / ".library" / "folders.json"
        if folders_file.is_file():
            archive.write(folders_file, arcname=".library/folders.json")
        for course in sorted(item for item in root.iterdir() if item.is_dir()):
            if course.name.startswith("."):
                continue
            _zip_tree(course, archive, arc_prefix=course.name)
    return destination


def list_backups(directory: str | Path) -> list[Path]:
    base = Path(directory)
    return sorted(base.glob("*.zip")) if base.is_dir() else []
