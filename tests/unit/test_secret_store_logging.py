"""密钥存储与日志脱敏的单元测试（《技术文档》§3.9、§6；《项目文档》§14.2、T-07）。"""

from __future__ import annotations

import logging

from app.core.config import default_config
from app.infra.logging import RedactingFilter, configure_logging, redact, shutdown_logging
from app.infra.secret_store import (
    ENV_API_KEY,
    ENV_BASE_URL,
    InMemoryBackend,
    SecretStore,
    keyring_backend_available,
    mask_secret,
    ref_for_profile,
    resolve_api_key,
    resolve_base_url,
)


def test_secret_roundtrip_with_memory_backend() -> None:
    store = SecretStore(backend=InMemoryBackend())

    assert store.get_secret("atm/x") is None
    store.set_secret("atm/x", "sk-secret")
    assert store.get_secret("atm/x") == "sk-secret"
    assert store.has_secret("atm/x") is True
    assert store.masked("atm/x") == "sk-****cret"
    assert store.delete_secret("atm/x") is True
    assert store.get_secret("atm/x") is None
    assert store.has_secret("atm/x") is False


def test_mask_secret_variants() -> None:
    assert mask_secret(None) == ""
    assert mask_secret("") == ""
    assert mask_secret("abcd") == "****"
    assert mask_secret("sk-abcdefghijklmnop") == "sk-****mnop"
    assert mask_secret("12345678") == "****5678"


def test_ref_for_profile_uses_secret_ref() -> None:
    profile = default_config().require_profile()
    assert ref_for_profile(profile) == "atm/deepseek-default"

    profile.secret_ref = None
    assert ref_for_profile(profile) == "atm/deepseek-default"


def test_resolve_api_key_env_has_priority_and_is_not_persisted() -> None:
    store = SecretStore(backend=InMemoryBackend())
    store.set_secret("atm/deepseek-default", "sk-from-store")
    profile = default_config().require_profile()

    assert resolve_api_key(profile, store, env={}) == "sk-from-store"
    assert resolve_api_key(profile, store, env={ENV_API_KEY: "sk-from-env"}) == "sk-from-env"
    assert resolve_api_key(profile, store, env={ENV_API_KEY: "   "}) == "sk-from-store"


def test_resolve_base_url_env_has_priority() -> None:
    profile = default_config().require_profile()
    assert resolve_base_url(profile, env={}) == "https://api.deepseek.com/v1"
    assert resolve_base_url(profile, env={ENV_BASE_URL: "http://localhost:11434/v1"}) == (
        "http://localhost:11434/v1"
    )


def test_keyring_backend_available_returns_bool() -> None:
    assert isinstance(keyring_backend_available(), bool)


def test_redact_replaces_known_secret_shapes() -> None:
    assert "sk-abcdefghijklmnop" not in redact("Authorization: Bearer sk-abcdefghijklmnop")
    assert "***" in redact("Authorization: Bearer sk-abcdefghijklmnop")
    assert "***" in redact('{"api_key": "abcdef123456"}')
    assert "abcdef123456" not in redact('{"api_key": "abcdef123456"}')
    assert redact("普通日志：今天讲了矩阵乘法") == "普通日志：今天讲了矩阵乘法"


def test_log_file_is_redacted(tmp_path) -> None:
    logger = configure_logging(tmp_path, retention_days=7)
    try:
        logger.info("token=%s", "sk-abcdefghijklmnop")
        for handler in logger.handlers:
            handler.flush()
        text = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert "sk-abcdefghijklmnop" not in text
        assert "***" in text
    finally:
        shutdown_logging(logger)


def test_configure_logging_does_not_duplicate_handlers(tmp_path) -> None:
    first = configure_logging(tmp_path)
    assert len(first.handlers) == 1
    second = configure_logging(tmp_path)
    assert len(second.handlers) == 1
    shutdown_logging(second)


def test_redacting_filter_clears_args() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "key=%s", ("sk-abcdefghij",), None)
    RedactingFilter().filter(record)
    assert record.args == ()
    assert "sk-abcdefghij" not in record.getMessage()
