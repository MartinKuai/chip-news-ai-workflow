"""Circuit breaker for consecutive Gemini service failures during a run."""

from __future__ import annotations

from collections import deque
from enum import StrEnum


class HealthEvent(StrEnum):
    """One Gemini AI-stage outcome recorded into the rolling window."""

    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient"
    NON_TRANSIENT_FAILURE = "non_transient"

    @property
    def mark(self) -> str:
        return {
            HealthEvent.SUCCESS: "S",
            HealthEvent.TRANSIENT_FAILURE: "T",
            HealthEvent.NON_TRANSIENT_FAILURE: "N",
        }[self]


class ServiceHealth:
    """Track the last ``window_size`` Gemini outcomes and open on a failure threshold.

    Only transient infrastructure failures (429 / 5xx / network / timeout) count
    toward the threshold. Non-transient article failures such as SchemaError or
    GeminiResponseError occupy a slot but neither count as transient failures nor
    erase the earlier transient history.
    """

    def __init__(self, window_size: int = 5, failure_threshold: int = 3) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if not 1 <= failure_threshold <= window_size:
            raise ValueError("failure_threshold must be between 1 and window_size")
        self._window_size = window_size
        self._failure_threshold = failure_threshold
        self._events: deque[HealthEvent] = deque(maxlen=window_size)

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    @property
    def events(self) -> tuple[HealthEvent, ...]:
        return tuple(self._events)

    @property
    def observed(self) -> int:
        return len(self._events)

    @property
    def transient_count(self) -> int:
        return sum(
            1 for event in self._events if event is HealthEvent.TRANSIENT_FAILURE
        )

    @property
    def window_full(self) -> bool:
        return len(self._events) >= self._window_size

    @property
    def is_open(self) -> bool:
        return self.window_full and self.transient_count >= self._failure_threshold

    def record(self, event: HealthEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> str:
        """Return a log-safe window state, e.g. ``window=[T,T,S,N,S] transient=2/5``."""
        marks = ",".join(event.mark for event in self._events)
        return (
            f"window=[{marks}] transient={self.transient_count}/{self.observed} "
            f"threshold={self._failure_threshold}"
        )
