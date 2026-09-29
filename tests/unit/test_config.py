"""配置读写的单元测试（《技术文档》§3.5.5、§18.6；《项目文档》T-42）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import (
    SCHEMA_VERSION,
    SEARCH_BACKENDS,
    SEARCH_BACKEND_TYPES,
    AppConfig,
    config_file,
    default_config,
    extract_plaintext_secrets,
    load_config,
    migrate_config,
    save_config,
)
from app.core.errors import ConfigError, ConfigTooNew


class _FakeStore:
    """最小凭据库替身（只实现 set_secret）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def set_secret(self, ref: str, value: str) -> None:
        self.data[ref] = value


def test_default_config_matches_doc_defaults() -> None:
    config = default_config("D:/ws")

    assert config.schema_version == SCHEMA_VERSION
    assert config.workspace_root == "D:/ws"
    assert config.active_profile == "deepseek-default"
    assert config.profile_names() == ["deepseek-default"]

    profile = config.require_profile()
    assert profile.base_url == "https://api.deepseek.com/v1"
    assert profile.chat_model == "deepseek-flash"
    assert profile.vision_model is None
    assert profile.capabilities.vision is True
    assert profile.capabilities.hosted_web_search is False

    assert config.routing.web_search is None
    assert config.ui.theme == "sakura"
    assert 30 <= config.generation.stream_throttle_ms <= 60
    assert config.generation.svg_review.max_rounds == 3
    assert config.searching_model.enabled is False
    assert config.searching_model.backend == "qianfan"
    assert config.searching_model.usage == "references_only"
    assert config.privacy.telemetry is False


def test_load_config_creates_default_file(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    path = config_file(tmp_path)
    assert path == (tmp_path / "data" / "config.json").resolve()
    assert path.exists()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["workspace_root"] == str(tmp_path)
    assert config.active_profile == "deepseek-default"


def test_save_config_roundtrip(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    config.ui.theme = "forest"
    config.ui.font_size = 18
    config.generation.default_depth = "deep"
    save_config(tmp_path, config)

    reloaded = load_config(tmp_path)
    assert reloaded.ui.theme == "forest"
    assert reloaded.ui.font_size == 18
    assert reloaded.generation.default_depth == "deep"


def test_unknown_theme_is_rejected(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    config.ui.theme = "rainbow"
    save_config(tmp_path, config)

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_unknown_search_backend_is_rejected(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    config.searching_model.backend = "bing"
    save_config(tmp_path, config)

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_config_too_new_raises(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8")

    with pytest.raises(ConfigTooNew):
        load_config(tmp_path)


def test_broken_json_raises_config_error(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_migrate_config_fills_version_and_notes() -> None:
    raw, notes = migrate_config({"active_profile": "x"})
    assert raw["schema_version"] == SCHEMA_VERSION
    assert notes and "迁移" in notes[0]


def test_extract_plaintext_secrets_migrates_and_clears(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    config.require_profile().api_key = "sk-profile-plain"
    config.searching_model.api_key = "sk-search-plain"

    store = _FakeStore()
    migrated = extract_plaintext_secrets(config, store)

    assert migrated == ["atm/deepseek-default", "atm/qianfan-search"]
    assert store.data["atm/deepseek-default"] == "sk-profile-plain"
    assert store.data["atm/qianfan-search"] == "sk-search-plain"
    assert config.require_profile().api_key == ""
    assert config.searching_model.api_key == ""

    save_config(tmp_path, config)
    text = config_file(tmp_path).read_text(encoding="utf-8")
    assert "sk-profile-plain" not in text
    assert "sk-search-plain" not in text


def test_backend_type_mapping_covers_all_backends() -> None:
    assert set(SEARCH_BACKEND_TYPES) == set(SEARCH_BACKENDS)
    config = default_config()
    for backend, expected in SEARCH_BACKEND_TYPES.items():
        config.searching_model.backend = backend
        assert config.searching_model.backend_type == expected


def test_require_profile_raises_for_unknown_name() -> None:
    config = default_config()
    with pytest.raises(ConfigError):
        config.require_profile("nope")


def test_app_config_dump_is_json_safe() -> None:
    dumped: dict[str, object] = default_config("D:/ws").model_dump(mode="json")
    text = json.dumps(dumped, ensure_ascii=False)
    assert AppConfig.model_validate(json.loads(text)).active_profile == "deepseek-default"

def test_legacy_default_max_tokens_is_bumped(tmp_path: Path) -> None:
    """旧默认 4096 在推理模型上会把正文顶掉（实测 finish_reason=length），
    只把「从没改过」的档案升到新默认，并落盘。"""
    from app.core.config import DEFAULT_MAX_TOKENS, LEGACY_DEFAULT_MAX_TOKENS

    config = default_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["llm_profiles"][0]["max_tokens"] = LEGACY_DEFAULT_MAX_TOKENS
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_config(tmp_path)  # 启动路径：迁移结果要落盘

    assert loaded.require_profile().max_tokens == DEFAULT_MAX_TOKENS
    assert json.loads(path.read_text(encoding="utf-8"))["llm_profiles"][0]["max_tokens"] == DEFAULT_MAX_TOKENS


def test_customized_max_tokens_is_left_alone(tmp_path: Path) -> None:
    config = default_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["llm_profiles"][0]["max_tokens"] = 8192
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    assert load_config(tmp_path, create=False).require_profile().max_tokens == 8192