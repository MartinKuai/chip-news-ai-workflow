from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.outcomes import RunOutcome, decide_run_outcome, exit_code_for


class DecisionTests(unittest.TestCase):
    def decide(self, **overrides) -> RunOutcome:
        arguments = {
            "published": 0,
            "failed": 0,
            "breaker_opened": False,
            "budget_exceeded": False,
            "force_failed": False,
        }
        arguments.update(overrides)
        return decide_run_outcome(**arguments)

    def test_five_successful_articles_are_success(self) -> None:
        self.assertIs(RunOutcome.SUCCESS, self.decide(published=5))

    def test_partial_failures_after_publishing_are_partial_success(self) -> None:
        self.assertIs(
            RunOutcome.PARTIAL_SUCCESS,
            self.decide(published=3, failed=2),
        )

    def test_breaker_after_publishing_is_partial_success(self) -> None:
        self.assertIs(
            RunOutcome.PARTIAL_SUCCESS,
            self.decide(published=1, failed=2, breaker_opened=True),
        )

    def test_no_failures_and_nothing_published_is_empty_success(self) -> None:
        self.assertIs(RunOutcome.EMPTY_SUCCESS, self.decide())

    def test_budget_exceeded_without_publication_is_failed(self) -> None:
        self.assertIs(
            RunOutcome.FAILED,
            self.decide(published=0, budget_exceeded=True),
        )

    def test_system_failures_without_publication_are_failed(self) -> None:
        self.assertIs(RunOutcome.FAILED, self.decide(published=0, failed=2))

    def test_program_fault_forces_failed_even_after_publishing(self) -> None:
        self.assertIs(RunOutcome.FAILED, self.decide(published=2, force_failed=True))

    def test_exit_codes_match_github_semantics(self) -> None:
        self.assertEqual(0, exit_code_for(RunOutcome.SUCCESS))
        self.assertEqual(0, exit_code_for(RunOutcome.PARTIAL_SUCCESS))
        self.assertEqual(0, exit_code_for(RunOutcome.EMPTY_SUCCESS))
        self.assertEqual(1, exit_code_for(RunOutcome.FAILED))


if __name__ == "__main__":
    unittest.main()
