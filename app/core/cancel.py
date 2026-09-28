"""取消令牌（《技术文档》§2.8「进程与线程」、§4.4.1 取消语义）。

取消语义：取消请求 → 关闭连接 → **保留已完成块** → 小节标记为未完成。
所以 cancel() 会同步触发已注册回调（供 app/llm/client.py 关闭流式连接），且保持幂等。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .errors import Cancelled


class CancellationToken:
    """线程安全的取消令牌。"""

    __slots__ = ("_lock", "_event", "_callbacks")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        """请求取消；首次调用返回 True，重复调用返回 False。"""
        with self._lock:
            if self._event.is_set():
                return False
            self._event.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue
        return True

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册取消回调；已取消时立即执行。返回反注册函数。"""
        if self.cancelled:
            callback()
            return lambda: None
        with self._lock:
            self._callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return unsubscribe

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled("任务已取消")

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    @classmethod
    def never(cls) -> "CancellationToken":
        """永不取消的令牌（用于把 cancel 参数写成可选）。"""
        return _NeverToken()

    def __bool__(self) -> bool:  # 语义：Truthy 表示「可以继续」
        return not self.cancelled

    def __repr__(self) -> str:
        return f"<CancellationToken cancelled={self.cancelled}>"


class _NeverToken(CancellationToken):
    def cancel(self) -> bool:
        return False
