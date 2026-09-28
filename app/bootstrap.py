"""工作区初始化与迁移检查（《技术文档》§3.2 / §3.11、《项目文档》§16.1 M0）。

职责：

1. 解析工作区根目录（默认仓库根目录，可用环境变量 ``ATM_WORKSPACE`` 覆盖）；
2. 首次启动创建 ``Courses/`` ``personas/`` ``data/`` ``data/cache`` ``data/logs`` ``exports/``；
3. 丢弃上次写入中途崩溃残留的 ``*.tmp``（§3.11）；
4. 把明文密钥迁入系统凭据库（T-42）并按需写回配置；
5. 装载内置示例课程：**逐文件比对，已存在的文件不覆盖**（§3.11 第 4 条 / AC-51）；
6. 检查 ``schema_version``：高于应用支持版本时进入只读模式（§18.6）。
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .core import config as config_module
from .core.config import AppConfig, extract_plaintext_secrets, load_config, save_config
from .core.errors import ConfigTooNew
from .core.paths import cleanup_stale_tmp, read_json, safe_component, safe_join
from .infra.logging import configure_logging
from .infra.secret_store import SecretStore, resolve_api_key

WORKSPACE_ENV = "ATM_WORKSPACE"
COURSES_DIR_NAME = "Courses"
PERSONAS_DIR_NAME = "personas"
DATA_DIR_NAME = "data"
EXPORTS_DIR_NAME = "exports"
CACHE_DIR_NAME = "cache"
LOGS_DIR_NAME = "logs"


@dataclass(frozen=True)
class WorkspaceLayout:
    """工作区目录布局（《技术文档》§3.2）。"""

    root: Path
    courses: Path
    personas: Path
    data: Path
    cache: Path
    logs: Path
    exports: Path
    config_file: Path

    @property
    def db_file(self) -> Path:
        return self.data / "library.db"


@dataclass
class BootstrapResult:
    """一次启动引导的结果（供界面提示与测试断言）。"""

    layout: WorkspaceLayout
    config: AppConfig
    created_dirs: list[Path] = field(default_factory=list)
    cleaned_tmp: list[Path] = field(default_factory=list)
    seeded_files: list[Path] = field(default_factory=list)
    migrated_secrets: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    read_only: bool = False
    needs_api_setup: bool = False


def default_workspace_root() -> Path:
    """默认工作区 = 仓库根目录（``app/bootstrap.py`` 的上两级）。"""
    return Path(__file__).resolve().parents[1]


def workspace_root_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    import os

    environment = os.environ if env is None else env
    raw = (environment.get(WORKSPACE_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def resolve_workspace_root(explicit: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = workspace_root_from_env(env)
    if from_env is not None:
        return from_env.resolve()
    return default_workspace_root()


def layout_for(root: str | Path) -> WorkspaceLayout:
    base = Path(root).resolve()
    return WorkspaceLayout(
        root=base,
        courses=base / COURSES_DIR_NAME,
        personas=base / PERSONAS_DIR_NAME,
        data=base / DATA_DIR_NAME,
        cache=base / DATA_DIR_NAME / CACHE_DIR_NAME,
        logs=base / DATA_DIR_NAME / LOGS_DIR_NAME,
        exports=base / EXPORTS_DIR_NAME,
        config_file=base / DATA_DIR_NAME / config_module.CONFIG_FILE_NAME,
    )


def ensure_workspace(root: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> WorkspaceLayout:
    """只创建目录（不读配置、不装示例课程）。"""
    layout = layout_for(resolve_workspace_root(root, env=env))
    for directory in _workspace_dirs(layout):
        directory.mkdir(parents=True, exist_ok=True)
    return layout


def _workspace_dirs(layout: WorkspaceLayout) -> tuple[Path, ...]:
    return (layout.courses, layout.personas, layout.data, layout.cache, layout.logs, layout.exports)


def seed_courses_dir() -> Path:
    """内置示例课程目录（随包分发）。"""
    return Path(__file__).resolve().parent / "ui" / "assets" / "seed_courses"


def list_seed_courses(source: str | Path | None = None) -> list[str]:
    root = Path(source) if source else seed_courses_dir()
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def install_seed_courses(
    layout: WorkspaceLayout,
    *,
    source: str | Path | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """把内置示例课程复制到 ``Courses/``；已存在的文件默认不动（AC-51）。"""
    root = Path(source) if source else seed_courses_dir()
    written: list[Path] = []
    if not root.is_dir():
        return written

    for course_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        target_root = safe_join(layout.courses, safe_component(course_dir.name))
        for path in sorted(course_dir.rglob("*")):
            relative = path.relative_to(course_dir)
            target = safe_join(target_root, *[safe_component(part) for part in relative.parts])
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            written.append(target)
    return written


def needs_api_setup(
    config: AppConfig,
    *,
    store: SecretStore | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """是否还没有任何可用密钥（FR-01 / AC-02 的首启动引导判定）。"""
    secrets = store if store is not None else SecretStore()
    for profile in config.llm_profiles:
        if resolve_api_key(profile, secrets, env=env):
            return False
    return True


def check_migrations(layout: WorkspaceLayout) -> list[str]:
    """检查配置文件的 ``schema_version``（§18.6）。返回需要提示的问题。"""
    issues: list[str] = []
    if layout.config_file.exists():
        try:
            raw = read_json(layout.config_file, default={}) or {}
        except ValueError as exc:
            issues.append(f"配置文件无法解析：{exc}")
            return issues
        version = raw.get("schema_version", 0) if isinstance(raw, dict) else 0
        if isinstance(version, int) and version > config_module.SCHEMA_VERSION:
            issues.append(
                f"配置 schema_version={version} 高于应用支持的 {config_module.SCHEMA_VERSION}："
                "进入只读模式，请升级应用"
            )
    return issues


def bootstrap_workspace(
    root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    store: SecretStore | None = None,
    install_seed: bool = True,
    configure_file_logging: bool = True,
    retention_days: int | None = None,
) -> BootstrapResult:
    """应用启动时的完整引导流程。"""
    layout = layout_for(resolve_workspace_root(root, env=env))
    created: list[Path] = []
    for directory in _workspace_dirs(layout):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)

    cleaned = cleanup_stale_tmp(layout.data) + cleanup_stale_tmp(layout.courses)

    messages: list[str] = []
    read_only = False
    try:
        config = load_config(layout.root)
    except ConfigTooNew as exc:
        read_only = True
        messages.append(f"{exc}（已进入只读模式，可浏览与导出，暂不允许写入）")
        config = config_module.default_config(layout.root)

    secrets = store if store is not None else SecretStore()
    migrated: list[str] = []
    if not read_only:
        try:
            migrated = extract_plaintext_secrets(config, secrets)
        except Exception as exc:  # noqa: BLE001 - 凭据库不可用时不能让应用起不来
            messages.append(f"明文密钥迁移失败：{exc}")
        if migrated:
            save_config(layout.root, config)
            messages.append(f"已把 {len(migrated)} 个明文密钥迁入系统凭据库")

    seeded: list[Path] = []
    if install_seed:
        try:
            seeded = install_seed_courses(layout)
        except Exception as exc:  # noqa: BLE001
            messages.append(f"示例课程装载失败：{exc}")

    if configure_file_logging:
        retention = retention_days if retention_days is not None else config.privacy.log_retention_days
        configure_logging(layout.logs, retention_days=retention)

    return BootstrapResult(
        layout=layout,
        config=config,
        created_dirs=created,
        cleaned_tmp=cleaned,
        seeded_files=seeded,
        migrated_secrets=migrated,
        messages=messages,
        read_only=read_only,
        needs_api_setup=needs_api_setup(config, store=secrets, env=env),
    )
