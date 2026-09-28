"""schema_version 迁移（《技术文档》§3.11、《项目文档》§18.6）。

约定：版本相同→正常读写；版本更低→**先备份**再逐级迁移；版本更高→只读模式。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import config as config_module
from ..config import SCHEMA_VERSION
from ..errors import ConfigTooNew
from ..paths import read_json

Migrator = Callable[[dict[str, Any]], dict[str, Any]]


def _v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """无版本号的早期配置：补 schema_version 与默认分组。"""
    migrated = dict(raw)
    migrated.setdefault("schema_version", 1)
    migrated.setdefault("llm_profiles", [])
    migrated.setdefault("routing", {})
    migrated.setdefault("ui", {})
    return migrated


CONFIG_MIGRATIONS: dict[int, Migrator] = {0: _v0_to_v1}


def migrate_config_dict(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """配置迁移（对外仍由 ``core.config.migrate_config`` 调用，保持单一入口）。"""
    return config_module.migrate_config(raw)


def course_schema_versions(course_dir: str | Path) -> dict[str, int]:
    root = Path(course_dir)
    versions: dict[str, int] = {}
    for path in root.rglob("*.json"):
        data = read_json(path, default={}) or {}
        if isinstance(data, dict):
            versions[str(path.relative_to(root))] = int(data.get("schema_version") or 0)
    return versions


def check_course(course_dir: str | Path, *, supported: int = SCHEMA_VERSION) -> list[str]:
    """课程层面的迁移提示；版本过高即要求只读（§18.6）。"""
    return [
        f"{name} 的 schema_version={version} 高于应用支持的 {supported}：该课程已置为只读"
        for name, version in course_schema_versions(course_dir).items()
        if version > supported
    ]


def migrate_course(course_dir: str | Path, *, supported: int = SCHEMA_VERSION) -> list[str]:
    """逐级迁移课程 JSON（先备份 ``.bak``，再写回新版本）。"""
    from ...infra import file_repository

    root = Path(course_dir)
    notes: list[str] = []
    for path in root.rglob("*.json"):
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            continue
        version = int(data.get("schema_version") or 0)
        if version >= supported:
            continue
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        migrated = data
        for step in range(version, supported):
            migrator = CONFIG_MIGRATIONS.get(step)
            if migrator is not None:
                migrated = migrator(migrated)
        migrated["schema_version"] = supported
        file_repository.write_json(path, migrated)
        notes.append(f"{path.name}: v{version} → v{supported}（原文件已备份为 {backup.name}）")
    return notes


__all__ = [
    "CONFIG_MIGRATIONS",
    "ConfigTooNew",
    "check_course",
    "course_schema_versions",
    "migrate_config_dict",
    "migrate_course",
]
