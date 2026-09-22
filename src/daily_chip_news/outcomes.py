"""Candidate-level and run-level outcomes."""

from __future__ import annotations

from enum import StrEnum


class CandidateStatus(StrEnum):
    """Simple per-candidate result; failures are tracked separately."""

    PUBLISHED = "PUBLISHED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class RunOutcome(StrEnum):
    """Final state of one run, derived from processed candidates."""

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


def exit_code_for(outcome: RunOutcome) -> int:
    return 1 if outcome is RunOutcome.FAILED else 0


def decide_run_outcome(
    *,
    published: int,
    failed: int,
    stopped_early: bool,
    force_failed: bool = False,
) -> RunOutcome:
    """Derive the run outcome from the processed candidate results.

    - ``SUCCESS``: nothing failed and the run was not cut short, even when every
      candidate was skipped by the editorial rules (no publishable content).
    - ``PARTIAL_SUCCESS``: something was published while failures or an early
      stop (breaker open / run budget reached) prevented a clean run.
    - ``FAILED``: nothing was published together with failures or an early stop.
    """
    if force_failed:
        return RunOutcome.FAILED
    if failed > 0 or stopped_early:
        return RunOutcome.PARTIAL_SUCCESS if published > 0 else RunOutcome.FAILED
    return RunOutcome.SUCCESS
