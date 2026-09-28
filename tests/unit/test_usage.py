"""用量统计的单元测试（《技术文档》§2.3 / §3.7）。"""

from __future__ import annotations

from app.llm.usage import UsageTracker


def _fixed_clock() -> str:
    return "2026-09-28T10:00:00+08:00"


def test_record_appends_and_stamps_time() -> None:
    tracker = UsageTracker(clock=_fixed_clock)
    entry = tracker.record(model="deepseek-flash", prompt_tokens=100, completion_tokens=20, task="teach")

    assert entry.ts == "2026-09-28T10:00:00+08:00"
    assert tracker.records == (entry,)
    assert entry.total_tokens == 120


def test_total_sums_tokens() -> None:
    tracker = UsageTracker(clock=_fixed_clock)
    tracker.record(model="a", prompt_tokens=10, completion_tokens=5)
    tracker.record(model="b", prompt_tokens=100, completion_tokens=50)

    assert tracker.total() == {
        "calls": 2,
        "prompt_tokens": 110,
        "completion_tokens": 55,
        "total_tokens": 165,
    }


def test_by_model_groups_records() -> None:
    tracker = UsageTracker(clock=_fixed_clock)
    tracker.record(model="a", prompt_tokens=10, completion_tokens=5)
    tracker.record(model="a", prompt_tokens=1, completion_tokens=1)
    tracker.record(model="b", prompt_tokens=2, completion_tokens=2)

    grouped = tracker.by_model()
    assert grouped["a"] == {"calls": 2, "prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17}
    assert grouped["b"]["calls"] == 1


def test_sink_receives_records() -> None:
    seen: list[dict[str, object]] = []
    tracker = UsageTracker(sink=lambda record: seen.append(record.as_dict()), clock=_fixed_clock)
    tracker.record(model="m", prompt_tokens=1, completion_tokens=2, lesson_id="L1-1")

    assert len(seen) == 1
    assert seen[0]["lesson_id"] == "L1-1"
    assert seen[0]["total_tokens"] == 3


def test_negative_tokens_are_clamped() -> None:
    tracker = UsageTracker(clock=_fixed_clock)
    entry = tracker.record(model="m", prompt_tokens=-5, completion_tokens=-1)
    assert entry.prompt_tokens == 0
    assert entry.completion_tokens == 0


def test_reset_clears_records() -> None:
    tracker = UsageTracker(clock=_fixed_clock)
    tracker.record(model="m", prompt_tokens=1)
    tracker.reset()
    assert tracker.records == ()
    assert tracker.total()["calls"] == 0
    assert tracker.by_model() == {}
