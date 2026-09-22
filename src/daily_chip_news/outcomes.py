"""Explicit run outcomes and their GitHub Actions exit codes."""

from __future__ import annotations

from enum import Enum


class RunOutcome(str, Enum):
    """Final state of one Daily Chip News run."""

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    EMPTY_SUCCESS = "EMPTY_SUCCESS"
    COST_GUARD_STOPPED = "COST_GUARD_STOPPED"
    FAILED = "FAILED"


def exit_code_for(outcome: RunOutcome) -> int:
    return 1 if outcome is RunOutcome.FAILED else 0


def decide_run_outcome(
    *,
    published: int,
    failed: int,
    breaker_opened: bool,
    budget_exceeded: bool,
    force_failed: bool,
    cost_guard_stopped: bool = False,
) -> RunOutcome:
    """Decide the run outcome without mixing article failures and workflow failures."""
    if force_failed:
        return RunOutcome.FAILED
    if cost_guard_stopped:
        # Protecting the budget is an expected operational state: GitHub Actions
        # must not paint it as an infrastructure failure.
        return RunOutcome.COST_GUARD_STOPPED
    if published > 0:
        if failed > 0 or breaker_opened or budget_exceeded:
            return RunOutcome.PARTIAL_SUCCESS
        return RunOutcome.SUCCESS
    if failed > 0 or breaker_opened or budget_exceeded:
        return RunOutcome.FAILED
    # No candidates, or every candidate was filtered by the editorial rules.
    return RunOutcome.EMPTY_SUCCESS
