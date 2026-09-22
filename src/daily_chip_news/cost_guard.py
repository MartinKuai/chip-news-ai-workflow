"""Pre-flight cost control for paid Gemini calls.

The guard authorizes every billable HTTP attempt *before* it is sent, reserves
the worst-case cost (counted input + output ceiling), and reconciles the
reservation with the actual usageMetadata afterwards. Ambiguous network
failures keep their reservation so the ledger can never undercount spend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .cost import (
    ModelPrice,
    PricingUnavailableError,
    UsageTotals,
    lookup_price,
    projected_call_cost_usd,
)

# Float comparison slack so an exactly-on-budget call is still allowed.
BUDGET_EPSILON_USD = 1e-9


class CostGuardStopReason(str, Enum):
    RUN_BUDGET = "run_budget"
    ROLLING_BUDGET = "rolling_budget"
    PRICING = "pricing"
    COUNT_TOKENS = "count_tokens"
    MISSING_OUTPUT_CEILING = "missing_output_ceiling"


class CostGuardExceeded(RuntimeError):
    """A paid call was refused before it could be sent.

    This is an expected operational state: it must not be retried, must not be
    classified as a Gemini service failure and must not pollute the health
    window.
    """

    def __init__(
        self,
        reason: CostGuardStopReason,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.details = details or {}
        super().__init__(f"cost guard: {reason.value}: {message}")


@dataclass
class Reservation:
    """One authorized (and reserved) billable attempt."""

    reservation_id: str
    model: str
    purpose: str
    projected_cost_usd: float
    state: str = "reserved"
    actual_cost_usd: float | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class CostGuardState:
    triggered: bool = False
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class CostGuard:
    """Track run/rolling spend and refuse any call that would exceed a budget."""

    def __init__(
        self,
        *,
        run_budget_usd: float,
        rolling_budget_usd: float,
        rolling_spend_usd: float = 0.0,
        recorder: Callable[[str, Reservation, UsageTotals | None], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if run_budget_usd <= 0:
            raise ValueError("run_budget_usd must be positive")
        if rolling_budget_usd <= 0:
            raise ValueError("rolling_budget_usd must be positive")
        self._run_budget = float(run_budget_usd)
        self._rolling_budget = float(rolling_budget_usd)
        self._run_spend = 0.0
        self._rolling_spend = max(0.0, float(rolling_spend_usd))
        self._recorder = recorder
        self._now = now
        self.state = CostGuardState()

    # --- observable state -------------------------------------------------
    @property
    def run_budget_usd(self) -> float:
        return self._run_budget

    @property
    def rolling_budget_usd(self) -> float:
        return self._rolling_budget

    @property
    def run_spend_usd(self) -> float:
        return self._run_spend

    @property
    def rolling_spend_usd(self) -> float:
        return self._rolling_spend

    @property
    def remaining_run_budget_usd(self) -> float:
        return max(0.0, self._run_budget - self._run_spend)

    @property
    def remaining_rolling_budget_usd(self) -> float:
        return max(0.0, self._rolling_budget - self._rolling_spend)

    # --- pricing ----------------------------------------------------------
    def price_for(self, model: str) -> ModelPrice:
        try:
            return lookup_price(model)
        except PricingUnavailableError as exc:
            self._stop(CostGuardStopReason.PRICING, {"model": model})
            raise CostGuardExceeded(
                CostGuardStopReason.PRICING,
                str(exc),
                details={"model": model},
            ) from exc

    # --- lifecycle --------------------------------------------------------
    def authorize(
        self,
        *,
        model: str,
        purpose: str,
        prompt_tokens: int,
        max_output_tokens: int | None,
    ) -> Reservation:
        """Reserve the worst-case cost of one billable attempt or refuse it."""
        if not max_output_tokens:
            self._stop(
                CostGuardStopReason.MISSING_OUTPUT_CEILING, {"model": model}
            )
            raise CostGuardExceeded(
                CostGuardStopReason.MISSING_OUTPUT_CEILING,
                f"model={model} has no output ceiling to bound its cost",
                details={"model": model},
            )
        price = self.price_for(model)
        projected = projected_call_cost_usd(
            prompt_tokens=prompt_tokens,
            max_output_tokens=max_output_tokens,
            price=price,
        )
        run_next = self._run_spend + projected
        rolling_next = self._rolling_spend + projected
        if run_next > self._run_budget + BUDGET_EPSILON_USD:
            details = {
                "projected_cost_usd": projected,
                "run_spend_usd": self._run_spend,
                "run_budget_usd": self._run_budget,
            }
            self._stop(CostGuardStopReason.RUN_BUDGET, details)
            raise CostGuardExceeded(
                CostGuardStopReason.RUN_BUDGET,
                f"projected ${projected:.6f} would exceed the run budget",
                details=details,
            )
        if rolling_next > self._rolling_budget + BUDGET_EPSILON_USD:
            details = {
                "projected_cost_usd": projected,
                "rolling_spend_usd": self._rolling_spend,
                "rolling_budget_usd": self._rolling_budget,
            }
            self._stop(CostGuardStopReason.ROLLING_BUDGET, details)
            raise CostGuardExceeded(
                CostGuardStopReason.ROLLING_BUDGET,
                f"projected ${projected:.6f} would exceed the rolling budget",
                details=details,
            )
        reservation = Reservation(
            reservation_id=uuid4().hex[:12],
            model=model,
            purpose=purpose or "n/a",
            projected_cost_usd=projected,
        )
        self._run_spend += projected
        self._rolling_spend += projected
        self._emit("reservation", reservation, None)
        return reservation

    def reconcile(self, reservation: Reservation, usage: UsageTotals | None) -> None:
        """Replace a reservation with the actual cost (or keep it if unknown)."""
        if reservation.state != "reserved":
            return
        if usage is None:
            # The response was billable but its usage cannot be read: keep the
            # conservative reservation instead of assuming a zero cost.
            reservation.state = "unresolved"
        else:
            refund = reservation.projected_cost_usd - usage.cost_usd
            self._run_spend = max(0.0, self._run_spend - refund)
            self._rolling_spend = max(0.0, self._rolling_spend - refund)
            reservation.actual_cost_usd = usage.cost_usd
            reservation.state = "consumed"
        self._emit("reconcile", reservation, usage)

    def release(self, reservation: Reservation) -> None:
        """Refund a reservation for an attempt that produced no generated tokens."""
        if reservation.state != "reserved":
            return
        self._run_spend = max(0.0, self._run_spend - reservation.projected_cost_usd)
        self._rolling_spend = max(
            0.0, self._rolling_spend - reservation.projected_cost_usd
        )
        reservation.state = "released"
        self._emit("release", reservation, None)

    def keep(self, reservation: Reservation) -> None:
        """Mark an ambiguous attempt (timeout) as still charged."""
        if reservation.state != "reserved":
            return
        reservation.state = "unresolved"
        self._emit("reconcile", reservation, None)

    # --- internals --------------------------------------------------------
    def _stop(self, reason: CostGuardStopReason, details: dict[str, Any]) -> None:
        self.state.triggered = True
        self.state.reason = reason.value
        self.state.details = dict(details)

    def _emit(
        self,
        event: str,
        reservation: Reservation,
        usage: UsageTotals | None,
    ) -> None:
        if self._recorder is not None:
            self._recorder(event, reservation, usage)
