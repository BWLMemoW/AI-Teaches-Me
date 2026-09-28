"""token 用量统计（《技术文档》§2.3 ``app/llm/usage.py``、§3.7 ``usage_log`` 表）。

M0 只做内存记账 + 可选 sink（落 SQLite 属 M1 的 infra/sqlite_index 职责）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class UsageRecord:
    """一次模型调用的用量账目。"""

    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ts: str = ""
    course_id: str | None = None
    lesson_id: str | None = None
    agent: str | None = None
    task: str | None = None
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "course_id": self.course_id,
            "lesson_id": self.lesson_id,
            "agent": self.agent,
            "task": self.task,
            "cached": self.cached,
        }


class UsageTracker:
    """内存用量账本；``sink`` 可接 SQLite / 日志（M1 起使用）。"""

    def __init__(
        self,
        *,
        sink: Callable[[UsageRecord], None] | None = None,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self._records: list[UsageRecord] = []
        self._sink = sink
        self._clock = clock

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        return tuple(self._records)

    def record(
        self,
        *,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        course_id: str | None = None,
        lesson_id: str | None = None,
        agent: str | None = None,
        task: str | None = None,
        cached: bool = False,
    ) -> UsageRecord:
        entry = UsageRecord(
            model=model,
            prompt_tokens=max(0, int(prompt_tokens)),
            completion_tokens=max(0, int(completion_tokens)),
            ts=self._clock(),
            course_id=course_id,
            lesson_id=lesson_id,
            agent=agent,
            task=task,
            cached=cached,
        )
        self._records.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return entry

    def total(self) -> dict[str, int]:
        return {
            "calls": len(self._records),
            "prompt_tokens": sum(item.prompt_tokens for item in self._records),
            "completion_tokens": sum(item.completion_tokens for item in self._records),
            "total_tokens": sum(item.total_tokens for item in self._records),
        }

    def by_model(self) -> dict[str, dict[str, int]]:
        buckets: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        for item in self._records:
            bucket = buckets[item.model]
            bucket["calls"] += 1
            bucket["prompt_tokens"] += item.prompt_tokens
            bucket["completion_tokens"] += item.completion_tokens
            bucket["total_tokens"] += item.total_tokens
        return dict(buckets)

    def reset(self) -> None:
        self._records.clear()
