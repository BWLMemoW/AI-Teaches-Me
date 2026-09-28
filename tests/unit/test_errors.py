"""异常分类与错误类型的单元测试（《技术文档》§4.4.1、《项目文档》§13.1）。"""

from __future__ import annotations

import pytest

from app.core.errors import (
    AuthError,
    ConfigError,
    ConfigTooNew,
    FatalError,
    InvalidName,
    PathEscape,
    ProviderError,
    RetryableError,
    classify_exception,
    is_retryable,
    status_of,
)


class _HttpError(Exception):
    """模拟 openai 的 APIStatusError（带 status_code 的任意异常）。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class _WithResponse(Exception):
    def __init__(self) -> None:
        super().__init__("bad gateway")
        self.response = type("Response", (), {"status_code": 502})()


def _named(name: str, **attrs: object) -> Exception:
    klass = type(name, (Exception,), {})
    exc = klass("boom")
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_named("AuthenticationError", status_code=401), AuthError),
        (_named("PermissionDeniedError", status_code=403), AuthError),
        (_HttpError(401), AuthError),
        (_HttpError(403), AuthError),
        (_named("RateLimitError", status_code=429), RetryableError),
        (_HttpError(429), RetryableError),
        (_HttpError(503), RetryableError),
        (_named("APITimeoutError"), RetryableError),
        (_named("ConnectError"), RetryableError),
        (_HttpError(400), FatalError),
        (_named("WeirdVendorError"), FatalError),
    ],
)
def test_classify_exception_maps_to_expected_type(exc: Exception, expected: type[ProviderError]) -> None:
    result = classify_exception(exc)
    assert isinstance(result, expected)
    assert result.retryable is expected.retryable


def test_auth_error_is_not_retryable_and_explains_in_chinese() -> None:
    error = classify_exception(_HttpError(401))
    assert isinstance(error, AuthError)
    assert is_retryable(error) is False
    assert "401" in str(error)
    assert "API Key" in error.explain()


def test_retryable_error_is_retryable() -> None:
    error = classify_exception(_HttpError(429))
    assert is_retryable(error) is True
    assert error.status == 429


def test_status_of_reads_response_status_code() -> None:
    assert status_of(_WithResponse()) == 502
    assert status_of(_named("AuthenticationError", status_code="401")) == 401
    assert status_of(Exception("no status")) is None


def test_classify_exception_is_idempotent_for_project_errors() -> None:
    error = RetryableError("自定义", detail="d", status=503)
    assert classify_exception(error) is error


def test_config_too_new_is_a_config_error() -> None:
    assert issubclass(ConfigTooNew, ConfigError)
    assert isinstance(ConfigTooNew("v9"), ValueError)
    assert "升级" in ConfigTooNew().explain()


def test_name_errors_carry_chinese_hint() -> None:
    assert InvalidName().explain() == "名称不合法，请换一个名字"
    assert "越界" in PathEscape().explain()
    assert FatalError().retryable is False
