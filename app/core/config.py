"""配置读写与默认值（《技术文档》§3.5.5 / §2.6、§18.6；《项目文档》§9.8）。

关键约定（违反即返工）：

1. ``secret_ref`` / ``api_key_ref`` **只存引用名**，真实密钥进系统凭据库；
2. 手写进配置的明文 ``api_key`` 在加载后立即迁入凭据库并清空该字段（T-42）；
3. 文件版本高于应用支持版本时抛 ``ConfigTooNew``，调用方进入只读模式（§18.6）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError, ConfigTooNew
from .paths import atomic_write_json, read_json, safe_join

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DATA_DIR_NAME = "data"
CONFIG_FILE_NAME = "config.json"

DEFAULT_PROFILE_NAME = "deepseek-default"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_CHAT_MODEL = "deepseek-flash"
DEFAULT_SECRET_REF = "atm/deepseek-default"
DEFAULT_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search"
DEFAULT_SEARCH_MODEL = "ernie-4.5-turbo-128k"
DEFAULT_SEARCH_SECRET_REF = "atm/qianfan-search"
# 推理型模型（如 deepseek-flash）会先花几千 token 思考，旧默认 4096 会把正文顶掉，
# 表现为「输出为空」或半截 JSON。这里记录旧值，供迁移识别「从未改过」的档案。
LEGACY_DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_TOKENS = 16384

# 《项目文档》§18.6 枚举速查
THEMES: tuple[str, ...] = ("deepspace", "paper", "sakura", "forest")
BACKGROUND_TYPES: tuple[str, ...] = ("image", "color", "gradient")
SEARCH_BACKENDS: tuple[str, ...] = (
    "qianfan",
    "openai_hosted",
    "tavily",
    "serper",
    "exa",
    "brave",
    "searxng",
)
SEARCH_USAGE: tuple[str, ...] = ("references_only", "answer_assist")
# 配置取值 ↔ 适配器类型（对应技术债 C-07）
SEARCH_BACKEND_TYPES: dict[str, str] = {
    "qianfan": "ai_search",
    "openai_hosted": "hosted",
    "tavily": "api",
    "serper": "api",
    "exa": "api",
    "brave": "api",
    "searxng": "self_hosted",
}


class SecretWriter(Protocol):
    """能写入系统凭据库的最小接口（真实实现见 app/infra/secret_store.py）。"""

    def set_secret(self, ref: str, value: str) -> None: ...


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vision: bool = True
    hosted_web_search: bool = False
    # 是否支持 function calling（默认档案 DeepSeek-flash 支持；由用户/探测结果维护，见 §3.5.5 约定 5）
    tools: bool = True


class LLMProfile(BaseModel):
    """一个模型供应商档案（《技术文档》§3.5.5）。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    base_url: str = DEFAULT_BASE_URL
    chat_model: str = DEFAULT_CHAT_MODEL
    # 留空表示「沿用对话模型」（§2.6 规则 2：DeepSeek 自带多模态读图）
    vision_model: str | None = None
    embedding_model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 16384
    timeout_seconds: int = 60
    proxy: str | None = None
    capabilities: Capabilities = Field(default_factory=Capabilities)
    secret_ref: str | None = None
    # 仅允许手写；加载后立即迁入凭据库（§3.5.5 约定 4、T-42）
    api_key: str = ""

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("档案 name 不能为空")
        return value.strip()

    @field_validator("temperature")
    @classmethod
    def _temperature_range(cls, value: float) -> float:
        if not 0.0 <= value <= 2.0:
            raise ValueError("temperature 必须在 0~2 之间")
        return value


class Routing(BaseModel):
    """按任务分发到不同供应商档案（《技术文档》§2.6）。"""

    model_config = ConfigDict(extra="ignore")

    teach: str | None = None
    outline: str | None = None
    illustrate: str | None = None
    vision_review: str | None = None
    # 仅当 searching_model.backend = "openai_hosted" 时才使用；默认 null
    web_search: str | None = None


class Background(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["image", "color", "gradient"] = "gradient"
    value: str | None = "linear-gradient(135deg, #FDF2F6 0%, #EEF2FF 100%)"
    path: str | None = None
    blur: float = 0.0
    dim: float = 0.1


class UIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    theme: str = "sakura"
    follow_system: bool = True
    font_family: str = "微软雅黑"
    font_size: int = 16
    line_height: float = 1.75
    reading_width: int = 820
    reduce_motion: bool = False
    background: Background = Field(default_factory=Background)

    @field_validator("theme")
    @classmethod
    def _known_theme(cls, value: str) -> str:
        if value not in THEMES:
            raise ValueError(f"未知主题 {value!r}，可选：{'/'.join(THEMES)}")
        return value


class PersonaConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default: str = "default-teacher"
    user_persona_dir: str = "personas"
    allow_roleplay: bool = True


class SvgReviewConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_rounds: int = 3
    render_width: int = 820
    scale: float = 2.0


class GenerationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_depth: Literal["minimal", "standard", "deep"] = "standard"
    auto_svg: bool = True
    auto_animation: bool = True
    auto_note_on_complete: bool = True
    quiz_enabled: bool = True
    self_assessment_enabled: bool = True
    # 是否允许 Agent 调用工具（function calling）。默认开启；端点/模型不支持时客户端会自动降级重试
    allow_tools: bool = True
    svg_review: SvgReviewConfig = Field(default_factory=SvgReviewConfig)
    # §2.8：流式回调节流 30–60ms
    stream_throttle_ms: int = 40


class SearchingModelConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    backend: str = "qianfan"
    url: str = DEFAULT_SEARCH_URL
    api_key: str = ""
    api_key_ref: str | None = DEFAULT_SEARCH_SECRET_REF
    default_model: str = DEFAULT_SEARCH_MODEL
    stream: bool = False
    top_k: int = 5
    usage: Literal["references_only", "answer_assist"] = "references_only"
    extra: dict[str, Any] = Field(
        default_factory=lambda: {"search_recency_filter": None, "resource_type_filter": ["web"]}
    )
    max_triggers_per_lesson: int = 2
    cache_ttl_hours: int = 24
    daily_call_budget: int = 100

    @field_validator("backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value not in SEARCH_BACKENDS:
            raise ValueError(f"未知搜索后端 {value!r}，可选：{'/'.join(SEARCH_BACKENDS)}")
        return value

    @field_validator("top_k")
    @classmethod
    def _top_k_range(cls, value: int) -> int:
        if not 1 <= value <= 20:
            raise ValueError("top_k 必须在 1~20 之间")
        return value

    @field_validator("daily_call_budget")
    @classmethod
    def _budget_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("daily_call_budget 不能为负")
        return value

    @property
    def backend_type(self) -> str:
        """适配器类型（《技术文档》§4.6 / §18.6 映射表）。"""
        return SEARCH_BACKEND_TYPES[self.backend]


class PrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    telemetry: bool = False
    log_retention_days: int = 30


class AppConfig(BaseModel):
    """``data/config.json`` 的完整结构。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    workspace_root: str = ""
    active_profile: str = DEFAULT_PROFILE_NAME
    llm_profiles: list[LLMProfile] = Field(default_factory=lambda: [default_profile()])
    routing: Routing = Field(default_factory=Routing)
    ui: UIConfig = Field(default_factory=UIConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    searching_model: SearchingModelConfig = Field(default_factory=SearchingModelConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    def profile(self, name: str | None = None) -> LLMProfile | None:
        """按名字取档案；``name`` 为空时取 ``active_profile``。"""
        wanted = name or self.active_profile
        for item in self.llm_profiles:
            if item.name == wanted:
                return item
        return None

    def require_profile(self, name: str | None = None) -> LLMProfile:
        profile = self.profile(name)
        if profile is None:
            raise ConfigError(f"找不到模型档案：{name or self.active_profile}")
        return profile

    def profile_names(self) -> list[str]:
        return [item.name for item in self.llm_profiles]


def default_profile() -> LLMProfile:
    return LLMProfile(name=DEFAULT_PROFILE_NAME, secret_ref=DEFAULT_SECRET_REF)


def default_config(workspace_root: str | Path = "") -> AppConfig:
    return AppConfig(workspace_root=str(workspace_root))


def config_file(workspace_root: str | Path) -> Path:
    return safe_join(workspace_root, DATA_DIR_NAME, CONFIG_FILE_NAME)


def config_from_dict(data: dict[str, Any]) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError("配置文件字段不合法", detail=str(exc)) from exc


def migrate_config(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """逐级迁移（当前只有 v1）；版本更高时抛 ``ConfigTooNew``。"""
    version = raw.get("schema_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigError("schema_version 必须是整数")
    if version > SCHEMA_VERSION:
        raise ConfigTooNew(f"配置 schema_version={version}，应用仅支持 {SCHEMA_VERSION}")
    notes: list[str] = []
    if version < SCHEMA_VERSION:
        raw = {**raw, "schema_version": SCHEMA_VERSION}
        notes.append(f"配置 schema_version 由 v{version} 迁移到 v{SCHEMA_VERSION}")
    return raw, notes


def normalize_legacy_max_tokens(raw: dict[str, Any]) -> list[str]:
    """把「从未改过」的旧默认 max_tokens 提升到新默认（用户改过就原样保留）。

    4096 是旧默认值，也是这批推理模型的失败阈值；只动恰好等于旧默认的档案，
    避免覆盖用户自己的调优。
    """
    notes: list[str] = []
    profiles = raw.get("llm_profiles")
    if not isinstance(profiles, list):
        return notes
    bumped = 0
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("max_tokens") == LEGACY_DEFAULT_MAX_TOKENS:
            profile["max_tokens"] = DEFAULT_MAX_TOKENS
            bumped += 1
    if bumped:
        notes.append(
            f"{bumped} 个模型档案的 max_tokens 由旧默认 {LEGACY_DEFAULT_MAX_TOKENS} "
            f"提升到 {DEFAULT_MAX_TOKENS}（推理模型思考会占用同一份预算）"
        )
    return notes


def load_config(workspace_root: str | Path, *, create: bool = True) -> AppConfig:
    """读取配置；缺失时按 ``create`` 写入默认配置。"""
    path = config_file(workspace_root)
    if not path.exists():
        config = default_config(workspace_root)
        if create:
            save_config(workspace_root, config)
        return config

    try:
        raw = read_json(path)
    except (ValueError, OSError) as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}", detail=str(exc)) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件内容必须是 JSON 对象：{path}")

    raw, notes = migrate_config(raw)
    notes.extend(normalize_legacy_max_tokens(raw))
    for note in notes:
        LOGGER.info("%s", note)
    config = config_from_dict(raw)
    if not config.workspace_root:
        config.workspace_root = str(workspace_root)
    if notes and create:
        try:
            save_config(workspace_root, config)  # 让迁移落盘，下次不必再算
        except OSError as exc:  # noqa: BLE001 - 写不进去也不能拦着启动
            LOGGER.warning("配置迁移结果写回失败：%s", exc)
    return config


def save_config(workspace_root: str | Path, config: AppConfig) -> Path:
    """原子写回配置（UTF-8 / LF / 不转义中文）。"""
    path = config_file(workspace_root)
    atomic_write_json(path, config.model_dump(mode="json"))
    return path


def extract_plaintext_secrets(config: AppConfig, store: SecretWriter) -> list[str]:
    """把配置里的明文密钥迁入凭据库并清空明文字段，返回被迁移的引用名。"""
    migrated: list[str] = []

    for profile in config.llm_profiles:
        if not profile.api_key:
            continue
        ref = profile.secret_ref or f"atm/{profile.name}"
        store.set_secret(ref, profile.api_key)
        profile.api_key = ""
        profile.secret_ref = ref
        migrated.append(ref)

    search = config.searching_model
    if search.api_key:
        ref = search.api_key_ref or DEFAULT_SEARCH_SECRET_REF
        store.set_secret(ref, search.api_key)
        search.api_key = ""
        search.api_key_ref = ref
        migrated.append(ref)

    return migrated
