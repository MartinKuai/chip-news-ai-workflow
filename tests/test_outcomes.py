from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401

from daily_chip_news.outcomes import (
    CandidateStatus,
    RunOutcome,
    decide_run_outcome,
    exit_code_for,
)


class CandidateStatusTests(unittest.TestCase):
    def test_statuses_are_the_simple_three(self) -> None:
        self.assertEqual(
            ["PUBLISHED", "SKIPPED", "FAILED"],
            [status.value for status in CandidateStatus],
        )


class RunOutcomeTests(unittest.TestCase):
    def test_success_requires_no_failures_and_no_early_stop(self) -> None:
        outcome = decide_run_outcome(published=3, failed=0, stopped_early=False)
        self.assertIs(RunOutcome.SUCCESS, outcome)

    def test_all_skipped_is_still_success(self) -> None:
        outcome = decide_run_outcome(published=0, failed=0, stopped_early=False)
        self.assertIs(RunOutcome.SUCCESS, outcome)

    def test_partial_success_needs_something_published(self) -> None:
        self.assertIs(
            RunOutcome.PARTIAL_SUCCESS,
            decide_run_outcome(published=1, failed=2, stopped_early=False),
        )
        self.assertIs(
            RunOutcome.PARTIAL_SUCCESS,
            decide_run_outcome(published=1, failed=0, stopped_early=True),
        )

    def test_failed_when_nothing_published_with_failures(self) -> None:
        self.assertIs(
            RunOutcome.FAILED,
            decide_run_outcome(published=0, failed=1, stopped_early=False),
        )
        self.assertIs(
            RunOutcome.FAILED,
            decide_run_outcome(published=0, failed=0, stopped_early=True),
        )

    def test_force_failed_overrides_published_items(self) -> None:
        self.assertIs(
            RunOutcome.FAILED,
            decide_run_outcome(
                published=4, failed=0, stopped_early=False, force_failed=True
            ),
        )

    def test_exit_codes(self) -> None:
        self.assertEqual(0, exit_code_for(RunOutcome.SUCCESS))
        self.assertEqual(0, exit_code_for(RunOutcome.PARTIAL_SUCCESS))
        self.assertEqual(1, exit_code_for(RunOutcome.FAILED))


if __name__ == "__main__":
    unittest.main()
