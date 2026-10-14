"""Per-model pacing and conservative, durable Pacific-day request budgets."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaStopped(RuntimeError):
    """Expected stop: a model's daily budget is unavailable."""

    def __init__(self, model: str, reason: str = "daily-request-budget") -> None:
        super().__init__(f"Gemini quota stop | model={model} | reason={reason}")
        self.model = model
        self.reason = reason


class QuotaStateError(RuntimeError):
    """State cannot safely be read or persisted; generation must not proceed."""


class QuotaWaitDeadline(RuntimeError):
    """The next permitted request would exceed the logical-call deadline."""


def quota_day(instant: datetime | None = None) -> str:
    return (instant or datetime.now(UTC)).astimezone(PACIFIC).date().isoformat()


def validate_ledger(document: object) -> dict:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise QuotaStateError("Invalid quota ledger version")
    day = document.get("quota_day")
    try:
        if (
            not isinstance(day, str)
            or datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day
        ):
            raise ValueError
    except ValueError:
        raise QuotaStateError("Invalid quota ledger day") from None
    counts = document.get("requests")
    blocked = document.get("blocked_models")
    if not isinstance(counts, dict) or any(
        not isinstance(model, str) or not model or type(count) is not int or count < 0
        for model, count in counts.items()
    ):
        raise QuotaStateError("Invalid quota ledger counters")
    if not isinstance(blocked, list) or any(
        not isinstance(model, str) or not model for model in blocked
    ):
        raise QuotaStateError("Invalid quota ledger blocked models")
    timestamps = document.get("next_allowed_at", {})
    if not isinstance(timestamps, dict):
        raise QuotaStateError("Invalid quota ledger pacing")
    for model, timestamp in timestamps.items():
        try:
            if (
                not isinstance(model, str)
                or not model
                or not isinstance(timestamp, str)
            ):
                raise ValueError
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                raise ValueError
        except ValueError:
            raise QuotaStateError("Invalid quota ledger pacing timestamp") from None
    for key in ("run_id", "run_attempt"):
        if key in document and (type(document[key]) is not int or document[key] < 1):
            raise QuotaStateError("Invalid quota ledger run metadata")
    return document


class QuotaGuard:
    """One instance is shared by all nodes, HTTP retries and JSON repair.

    Every attempted POST is reserved on disk *before* transmission. Failed or
    uncertain requests never release a reservation. Actions serializes runs and
    restores this file; local callers must also serialize access to the file.
    """

    def __init__(
        self,
        *,
        min_interval: float = 15.0,
        daily_limit: int = 18,
        ledger_path: str = "",
        required: bool = False,
        now: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        if not math.isfinite(min_interval) or min_interval < 0 or daily_limit < 1:
            raise ValueError("Invalid quota guard limits")
        self.min_interval = min_interval
        self.daily_limit = daily_limit
        self.path = Path(ledger_path) if ledger_path else None
        self.now, self.wall_clock, self.sleep, self.logger = (
            now,
            wall_clock,
            sleep,
            logger,
        )
        self.next_request: dict[str, float] = {}
        self.day = quota_day(wall_clock())
        self.requests: dict[str, int] = {}
        self.blocked: set[str] = set()
        self.context: dict = {}
        if required and (self.path is None or not self.path.is_file()):
            raise QuotaStateError("Required quota ledger is missing")
        if self.path is not None and self.path.exists():
            try:
                document = validate_ledger(
                    json.loads(self.path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                raise QuotaStateError("Quota ledger could not be read") from None
            if document["quota_day"] > self.day:
                raise QuotaStateError("Quota ledger is dated in the future")
            if document["quota_day"] == self.day:
                self.requests = dict(document["requests"])
                self.blocked = set(document["blocked_models"])
            self.context = {
                key: document[key]
                for key in ("run_id", "run_attempt", "bootstrap_note")
                if key in document
            }
            for model, timestamp in document.get("next_allowed_at", {}).items():
                wait = max(
                    0.0,
                    (
                        datetime.fromisoformat(timestamp) - self.wall_clock()
                    ).total_seconds(),
                )
                self.next_request[model] = self.now() + wait

    def _refresh_day(self) -> None:
        day = quota_day(self.wall_clock())
        if day < self.day:
            raise QuotaStateError("Quota clock moved backwards")
        if day != self.day:
            self.day, self.requests, self.blocked = day, {}, set()

    def snapshot(self) -> dict:
        return {
            **self.context,
            "version": 1,
            "quota_day": self.day,
            "daily_limit": self.daily_limit,
            "requests": dict(self.requests),
            "blocked_models": sorted(self.blocked),
            "next_allowed_at": {
                model: (
                    self.wall_clock() + timedelta(seconds=max(0, target - self.now()))
                ).isoformat()
                for model, target in self.next_request.items()
            },
        }

    def _persist(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.snapshot(), indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        except OSError:
            raise QuotaStateError("Quota reservation could not be persisted") from None

    def before_request(self, model: str, deadline: float) -> None:
        self._refresh_day()
        self._check_available(model)
        delay = max(0.0, self.next_request.get(model, 0.0) - self.now())
        if self.now() + delay >= deadline:
            raise QuotaWaitDeadline("Quota pacing would exceed the call deadline")
        if delay:
            if self.logger:
                self.logger(f"Gemini pacing | model={model} | wait={delay:.1f}s")
            self.sleep(delay)
        self._refresh_day()
        self._check_available(model)
        if self.now() >= deadline:
            raise QuotaWaitDeadline("Call deadline reached while waiting for quota")
        self.requests[model] = self.requests.get(model, 0) + 1
        self.next_request[model] = self.now() + self.min_interval
        self._persist()

    def _check_available(self, model: str) -> None:
        if model in self.blocked:
            raise QuotaStopped(model, "provider-daily-quota")
        if self.requests.get(model, 0) >= self.daily_limit:
            raise QuotaStopped(model)

    def block_daily(self, model: str) -> None:
        self._refresh_day()
        self.blocked.add(model)
        self._persist()

    def cooldown(self, model: str, seconds: float) -> None:
        self.next_request[model] = max(
            self.next_request.get(model, 0.0), self.now() + seconds
        )
        self._persist()
