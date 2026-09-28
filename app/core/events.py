"""应用内事件总线（《技术文档》§2.3 的 ``app/core/events.py``：Signal 封装）。

实现选择：这里用**纯标准库**的线程安全发布 / 订阅，Qt 侧再由 UI 适配成 Signal。
理由：《技术文档》§1.5 红线 2 要求服务层可无界面单测；若 core 直接依赖 PySide6，
services / agents 的测试就被迫装载 Qt。跨线程投递规则见 §2.8：工作线程只通过事件
把结果送回主线程，绝不直接触碰控件。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LOGGER = logging.getLogger(__name__)
WILDCARD = "*"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Event:
    """一条事件：``topic`` + 载荷 + 发生时间。"""

    topic: str
    payload: Any = None
    ts: str = field(default_factory=now_iso)

    def __str__(self) -> str:
        return f"{self.topic}@{self.ts}"


def topic_matches(pattern: str, topic: str) -> bool:
    """订阅模式匹配：``*`` 全匹配、``script:*`` 前缀匹配、其余精确匹配。"""
    if pattern == WILDCARD:
        return True
    if pattern.endswith(WILDCARD):
        return topic.startswith(pattern[:-1])
    return pattern == topic


class EventBus:
    """线程安全的发布 / 订阅总线。

    约定：

    1. ``emit`` 返回实际投递的订阅者数量（便于测试与诊断）；
    2. 单个订阅者抛异常不影响其它订阅者，异常交给 ``on_error``（默认写日志）；
    3. 订阅与投递都在锁外执行用户代码，避免重入死锁。
    """

    __slots__ = ("_lock", "_handlers", "_on_error")

    def __init__(
        self,
        *,
        on_error: Callable[[BaseException, Event], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}
        self._on_error = on_error or self._log_error

    @staticmethod
    def _log_error(exc: BaseException, event: Event) -> None:
        LOGGER.warning("事件 %s 的订阅者抛异常：%s", event.topic, exc)

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> Callable[[], None]:
        """订阅事件，返回反注册函数。"""
        if not topic:
            raise ValueError("topic 不能为空")
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)

        def unsubscribe() -> None:
            self.unsubscribe(topic, handler)

        return unsubscribe

    def unsubscribe(self, topic: str, handler: Callable[[Event], None]) -> bool:
        """反注册；返回是否真的移除了一个订阅。"""
        with self._lock:
            handlers = self._handlers.get(topic)
            if not handlers or handler not in handlers:
                return False
            handlers.remove(handler)
            if not handlers:
                self._handlers.pop(topic, None)
            return True

    def emit(self, topic: str, payload: Any = None) -> int:
        """投递事件，返回投递到的订阅者数量。"""
        event = Event(topic=topic, payload=payload)
        with self._lock:
            targets: list[Callable[[Event], None]] = []
            for pattern, handlers in self._handlers.items():
                if topic_matches(pattern, topic):
                    targets.extend(handlers)
        delivered = 0
        for handler in targets:
            try:
                handler(event)
            except BaseException as exc:  # noqa: BLE001 - 订阅者异常必须隔离
                self._on_error(exc, event)
                continue
            delivered += 1
        return delivered

    def subscriber_count(self, topic: str | None = None) -> int:
        with self._lock:
            if topic is None:
                return sum(len(handlers) for handlers in self._handlers.values())
            return len(self._handlers.get(topic, []))

    def patterns(self) -> list[str]:
        with self._lock:
            return sorted(self._handlers)

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
