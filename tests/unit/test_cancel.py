"""取消令牌的单元测试（《技术文档》§2.8 取消语义）。"""

from __future__ import annotations

import threading

import pytest

from app.core.cancel import CancellationToken
from app.core.errors import Cancelled


def test_new_token_is_not_cancelled() -> None:
    token = CancellationToken()
    assert token.cancelled is False
    assert bool(token) is True
    assert token.wait(0) is False


def test_cancel_is_idempotent() -> None:
    token = CancellationToken()
    assert token.cancel() is True
    assert token.cancel() is False
    assert token.cancelled is True
    assert bool(token) is False


def test_on_cancel_callback_runs_exactly_once() -> None:
    token = CancellationToken()
    calls: list[str] = []
    token.on_cancel(lambda: calls.append("closed"))
    token.cancel()
    token.cancel()
    assert calls == ["closed"]


def test_on_cancel_after_cancel_runs_immediately() -> None:
    token = CancellationToken()
    token.cancel()
    calls: list[str] = []
    token.on_cancel(lambda: calls.append("now"))
    assert calls == ["now"]


def test_unsubscribe_prevents_callback() -> None:
    token = CancellationToken()
    calls: list[str] = []
    off = token.on_cancel(lambda: calls.append("x"))
    off()
    token.cancel()
    assert calls == []


def test_callback_failure_does_not_break_cancel() -> None:
    token = CancellationToken()
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("boom")

    token.on_cancel(boom)
    token.on_cancel(lambda: calls.append("second"))
    assert token.cancel() is True
    assert calls == ["second"]


def test_throw_if_cancelled() -> None:
    token = CancellationToken()
    token.throw_if_cancelled()
    token.cancel()
    with pytest.raises(Cancelled):
        token.throw_if_cancelled()


def test_cancel_from_worker_thread() -> None:
    token = CancellationToken()
    thread = threading.Thread(target=token.cancel)
    thread.start()
    thread.join(timeout=5)
    assert token.cancelled is True
    assert token.wait(0) is True


def test_never_token_cannot_be_cancelled() -> None:
    token = CancellationToken.never()
    assert token.cancel() is False
    assert token.cancelled is False
