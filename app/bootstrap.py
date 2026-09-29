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
            # 老版本复制过来的示例课程：把新增章节树/进度/统计补上（不动用户内容）
            messages.extend(sync_seed_structure(layout))
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

# --- 依赖注入（《项目文档》§12：bootstrap 负责依赖注入） --------------------
@dataclass
class Services:
    """应用服务容器：UI 只拿这个，不自己 new 服务。"""

    result: BootstrapResult
    index: object
    client: object
    settings: object
    library: object
    outline: object
    course: object
    lesson: object
    search: object | None = None
    personas: object | None = None

    @property
    def layout(self) -> WorkspaceLayout:
        return self.result.layout

    @property
    def config(self) -> AppConfig:
        return self.result.config


def build_services(bootstrap_result: BootstrapResult, *, store: SecretStore | None = None) -> Services:
    """装配索引库 / LLM 客户端 / 各应用服务（依赖方向：UI → services → agents/infra）。"""
    from .infra.sqlite_index import SqliteIndex
    from .llm.client import LLMClient
    from .llm.usage import UsageTracker
    from .services import course_service, lesson_service
    from .services.library_service import LibraryService
    from .services.outline_service import OutlineService
    from .services.persona_service import PersonaService
    from .services.search_service import SearchService
    from .services.settings_service import SettingsService

    layout = bootstrap_result.layout
    index = SqliteIndex(layout.db_file)
    secrets = store if store is not None else SecretStore()

    def _usage_sink(record: object) -> None:
        index.log_usage(
            ts=str(getattr(record, "ts", "")),
            course_id=getattr(record, "course_id", None),
            lesson_id=getattr(record, "lesson_id", None),
            agent=getattr(record, "agent", None),
            model=str(getattr(record, "model", "")),
            prompt_tokens=int(getattr(record, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(record, "completion_tokens", 0) or 0),
        )

    usage = UsageTracker(sink=_usage_sink)
    client = LLMClient(bootstrap_result.config, secret_store=secrets, usage=usage)
    search_model = bootstrap_result.config.searching_model
    search_service = SearchService(
        cache_dir=layout.cache,
        searching_model=search_model,
        api_key=secrets.get_secret(search_model.api_key_ref) or "",
    )
    return Services(
        result=bootstrap_result,
        index=index,
        client=client,
        settings=SettingsService(bootstrap_result.config, workspace_root=layout.root, store=secrets, usage=usage),
        library=LibraryService(courses_dir=layout.courses, index=index),
        outline=OutlineService(courses_dir=layout.courses, client=client, index=index),
        course=course_service,
        lesson=lesson_service,
        search=search_service,
        personas=PersonaService(user_dir=layout.personas, index=index),
    )
def restore_seed_course(
    layout: WorkspaceLayout,
    *,
    course_name: str | None = None,
    source: str | Path | None = None,
) -> list[Path]:
    """恢复内置示例课程（《项目文档》§4.15：仅在设置中手动触发）。

    先删除现有示例课程（走回收站，可恢复），再从随包分发的种子里重新复制一份完整课程。
    """
    from .core.paths import safe_component
    from .infra import file_repository

    root = Path(source) if source else seed_courses_dir()
    names = [course_name] if course_name else list_seed_courses(root)
    written: list[Path] = []
    for name in names:
        target = safe_join(layout.courses, safe_component(str(name)))
        if target.exists():
            file_repository.delete_to_recycle_bin(target)  # 旧版进回收站，不直接抹掉
        written.extend(install_seed_courses(layout, source=root))
    return written
def sync_seed_structure(
    layout: WorkspaceLayout,
    *,
    course_name: str = "ATM 使用指南",
    source: str | Path | None = None,
) -> list[str]:
    """把内置种子的**结构**合并进已有示例课程：补章节树、进度与统计。

    只动结构文件（``outline.json`` / ``outline.md`` / ``chapter.json`` / ``progress.json`` /
    ``course.json`` 的 stats），**绝不覆盖**用户的讲义、笔记、lesson.json 与 widgets。
    """
    from .core.config import config_from_dict  # noqa: F401 - 保持导入风格一致
    from .core.models import CourseOutline, outline_to_markdown
    from .core.paths import read_json, safe_component, safe_join
    from .infra import file_repository

    root = Path(source) if source else seed_courses_dir()
    bundle = root / course_name
    target = safe_join(layout.courses, safe_component(course_name))
    if not bundle.is_dir() or not target.is_dir():
        return []

    notes: list[str] = []
    bundle_outline = read_json(bundle / "outline.json", default={}) or {}
    user_outline = read_json(target / "outline.json", default={}) or {}
    existing_ids = {
        lesson.get("id")
        for chapter in user_outline.get("chapters", []) or []
        for lesson in chapter.get("lessons", []) or []
    }

    changed = False
    for chapter in bundle_outline.get("chapters", []) or []:
        user_chapter = next(
            (item for item in user_outline.setdefault("chapters", []) if item.get("id") == chapter.get("id")),
            None,
        )
        if user_chapter is None:
            user_outline["chapters"].append(chapter)
            changed = True
            continue
        for lesson in chapter.get("lessons", []) or []:
            if lesson.get("id") not in existing_ids:
                user_chapter.setdefault("lessons", []).append(lesson)
                changed = True

    if changed:
        user_outline.setdefault("schema_version", 1)
        file_repository.write_json(target / "outline.json", user_outline)
        try:
            file_repository.write_text(
                target / "outline.md", outline_to_markdown(CourseOutline.model_validate(user_outline))
            )
        except Exception:  # noqa: BLE001 - 大纲可读版失败不影响结构
            pass
        notes.append("已补齐章节树")

    # 章元信息（缺哪个补哪个）
    for chapter in user_outline.get("chapters", []) or []:
        chapter_dir = target / "chapters" / safe_component(f"{chapter.get('id')}-{chapter.get('title', '')}")
        chapter_dir.mkdir(parents=True, exist_ok=True)
        meta = chapter_dir / "chapter.json"
        if not meta.exists():
            bundle_meta = bundle / "chapters" / f"{chapter.get('id')}-{chapter.get('title', '')}" / "chapter.json"
            if bundle_meta.exists():
                file_repository.write_text(meta, bundle_meta.read_text(encoding="utf-8"))
            else:
                file_repository.write_json(
                    meta,
                    {
                        "schema_version": 1,
                        "id": chapter.get("id"),
                        "title": chapter.get("title", ""),
                        "summary": chapter.get("summary", ""),
                        "lesson_order": [item.get("id") for item in chapter.get("lessons", []) or []],
                    },
                )
            notes.append(f"补齐 {chapter.get('id')} 的章信息")

    # 进度：补上缺失小节
    progress_path = target / "progress.json"
    progress = read_json(progress_path, default={}) or {}
    lessons = progress.setdefault("lessons", {})
    for chapter in user_outline.get("chapters", []) or []:
        for lesson in chapter.get("lessons", []) or []:
            lessons.setdefault(str(lesson.get("id")), {"status": "not_started", "mastery": None, "quiz_score": None, "minutes": 0})
    progress.setdefault("schema_version", 1)
    file_repository.write_json(progress_path, progress)

    # 课程统计
    total = sum(len(chapter.get("lessons", []) or []) for chapter in user_outline.get("chapters", []) or [])
    course_path = target / "course.json"
    course = read_json(course_path, default={}) or {}
    if course:
        course.setdefault("stats", {})
        course["stats"]["chapters"] = len(user_outline.get("chapters", []) or [])
        course["stats"]["lessons"] = total
        file_repository.write_json(course_path, course)
    # 课程资料文档：老课程也要有一份（本地拼装，不需要模型）
    materials_path = target / "materials" / "课程资料.md"
    if not materials_path.exists():
        from .core.models import CourseOutline, GOAL_LABELS
        from .services.outline_service import build_materials_from_outline

        try:
            course_outline = CourseOutline.model_validate(user_outline)
            goal_label = GOAL_LABELS.get(str(course.get("profile", {}).get("goal_type") or "general"), "")
            file_repository.write_text(
                materials_path, build_materials_from_outline(course_name, course_outline, goal_label)
            )
            notes.append("已生成课程资料文档")
        except Exception:  # noqa: BLE001
            pass

    notes.append(f"课程结构：{len(user_outline.get('chapters', []) or [])} 章 / {total} 节")
    return notes