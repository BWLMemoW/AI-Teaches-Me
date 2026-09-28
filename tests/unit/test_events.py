"""事件总线的单元测试（《技术文档》§2.3 / §2.8 跨线程投递）。"""

from __future__ import annotations

import threading

from app.core.events import Event, EventBus, topic_matches


def test_emit_delivers_payload_and_counts() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("script:block", seen.append)

    delivered = bus.emit("script:block", {"type": "text"})

    assert delivered == 1
    assert seen[0].topic == "script:block"
    assert seen[0].payload == {"type": "text"}
    assert seen[0].ts


def test_emit_without_subscriber_is_zero() -> None:
    assert EventBus().emit("nothing:here") == 0


def test_prefix_wildcard_matches_only_its_namespace() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("script:*", seen.append)

    assert bus.emit("script:block") == 1
    assert bus.emit("theme:changed") == 0
    assert [event.topic for event in seen] == ["script:block"]


def test_full_wildcard_matches_everything() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("*", seen.append)
    bus.emit("a")
    bus.emit("b:c")
    assert len(seen) == 2


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[Event] = []
    off = bus.subscribe("t", seen.append)
    bus.emit("t")
    off()
    bus.emit("t")
    assert len(seen) == 1
    assert bus.subscriber_count("t") == 0


def test_handler_exception_is_isolated_and_reported() -> None:
    errors: list[tuple[type[BaseException], str]] = []
    bus = EventBus(on_error=lambda exc, event: errors.append((type(exc), event.topic)))
    good: list[Event] = []

    def bad(event: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe("t", bad)
    bus.subscribe("t", good.append)

    delivered = bus.emit("t")

    assert delivered == 1
    assert len(good) == 1
    assert errors == [(RuntimeError, "t")]


def test_emit_from_worker_thread_delivers() -> None:
    bus = EventBus()
    got: list[object] = []
    done = threading.Event()

    def handler(event: Event) -> None:
        got.append(event.payload)
        done.set()

    bus.subscribe("job:done", handler)
    worker = threading.Thread(target=lambda: bus.emit("job:done", 42))
    worker.start()
    worker.join(timeout=5)

    assert done.wait(1) is True
    assert got == [42]


def test_patterns_and_clear() -> None:
    bus = EventBus()
    bus.subscribe("b", lambda event: None)
    bus.subscribe("a", lambda event: None)
    assert bus.patterns() == ["a", "b"]
    assert bus.subscriber_count() == 2
    bus.clear()
    assert bus.subscriber_count() == 0


def test_subscribe_requires_topic() -> None:
    bus = EventBus()
    try:
        bus.subscribe("", lambda event: None)
    except ValueError as exc:
        assert "topic" in str(exc)
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("空 topic 应当抛 ValueError")


def test_topic_matches_unit() -> None:
    assert topic_matches("*", "any:thing") is True
    assert topic_matches("script:*", "script:block") is True
    assert topic_matches("script:*", "scripts") is False
    assert topic_matches("exact", "exact") is True
    assert topic_matches("exact", "other") is False
