from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.cost import UsageTotals
from daily_chip_news.cost_guard import (
    CostGuard,
    CostGuardExceeded,
    CostGuardStopReason,
)


def price_probe_usage(cost: float) -> UsageTotals:
    return UsageTotals(
        prompt_tokens=1_000,
        candidate_tokens=200,
        thought_tokens=100,
        total_tokens=1_300,
        billable_requests=1,
        cost_usd=cost,
    )


class CostGuardTests(unittest.TestCase):
    def make_guard(self, **overrides) -> CostGuard:
        values = {
            "run_budget_usd": 0.20,
            "rolling_budget_usd": 7.50,
            "rolling_spend_usd": 0.0,
        }
        values.update(overrides)
        return CostGuard(**values)

    def authorize(self, guard: CostGuard, **overrides):
        arguments = {
            "model": "gemini-3.5-flash-lite",
            "purpose": "researcher",
            "prompt_tokens": 4_000,
            "max_output_tokens": 3_072,
        }
        arguments.update(overrides)
        return guard.authorize(**arguments)

    def test_authorization_reserves_the_projected_cost(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard)
        expected = 4_000 * 1.05 * 0.30 / 1_000_000 + 3_072 * 2.50 / 1_000_000
        self.assertAlmostEqual(expected, reservation.projected_cost_usd, places=9)
        self.assertAlmostEqual(expected, guard.run_spend_usd, places=9)
        self.assertAlmostEqual(expected, guard.rolling_spend_usd, places=9)
        self.assertEqual("reserved", reservation.state)
        self.assertFalse(guard.state.triggered)

    def test_run_budget_refuses_before_sending(self) -> None:
        guard = self.make_guard(scheduled=False) if False else self.make_guard()
        with self.assertRaises(CostGuardExceeded) as context:
            self.authorize(guard, prompt_tokens=1_000_000)
        self.assertIs(CostGuardStopReason.RUN_BUDGET, context.exception.reason)
        self.assertEqual(0.0, guard.run_spend_usd)
        self.assertTrue(guard.state.triggered)
        self.assertEqual("run_budget", guard.state.reason)

    def test_rolling_budget_refuses_before_sending(self) -> None:
        guard = self.make_guard(rolling_spend_usd=7.499)
        with self.assertRaises(CostGuardExceeded) as context:
            self.authorize(guard, prompt_tokens=100_000)
        self.assertIs(CostGuardStopReason.ROLLING_BUDGET, context.exception.reason)
        self.assertEqual(0.0, guard.run_spend_usd)

    def test_exactly_on_budget_is_allowed(self) -> None:
        guard = self.make_guard(run_budget_usd=0.0005632)
        reservation = self.authorize(
            guard,
            model="gemini-3.7-flash",
            prompt_tokens=0,
            max_output_tokens=125,
        )
        self.assertAlmostEqual(125 * 3.75 / 1_000_000, reservation.projected_cost_usd, places=12)

    def test_unknown_pricing_fails_closed(self) -> None:
        guard = self.make_guard()
        with self.assertRaises(CostGuardExceeded) as context:
            guard.price_for("gemini-9.9-unknown")
        self.assertIs(CostGuardStopReason.PRICING, context.exception.reason)
        self.assertTrue(guard.state.triggered)

    def test_missing_output_ceiling_fails_closed(self) -> None:
        guard = self.make_guard()
        with self.assertRaises(CostGuardExceeded) as context:
            self.authorize(guard, max_output_tokens=None)
        self.assertIs(
            CostGuardStopReason.MISSING_OUTPUT_CEILING, context.exception.reason
        )
        self.assertEqual(0.0, guard.run_spend_usd)

    def test_reconcile_replaces_the_reservation_with_the_actual_cost(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard)
        actual = price_probe_usage(0.0000123)
        guard.reconcile(reservation, actual)
        self.assertEqual("consumed", reservation.state)
        self.assertAlmostEqual(0.0000123, guard.run_spend_usd, places=9)
        self.assertAlmostEqual(0.0000123, guard.rolling_spend_usd, places=9)
        self.assertAlmostEqual(0.0000123, reservation.actual_cost_usd, places=9)

    def test_reconcile_never_undercounts_a_higher_actual_cost(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard)
        projected = reservation.projected_cost_usd
        actual = price_probe_usage(projected * 2)
        guard.reconcile(reservation, actual)
        self.assertAlmostEqual(projected * 2, guard.run_spend_usd, places=9)

    def test_reconcile_without_usage_keeps_the_reservation(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard)
        projected = reservation.projected_cost_usd
        guard.reconcile(reservation, None)
        self.assertEqual("unresolved", reservation.state)
        self.assertAlmostEqual(projected, guard.run_spend_usd, places=9)

    def test_keep_marks_a_timeout_as_still_charged(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard)
        projected = reservation.projected_cost_usd
        guard.keep(reservation)
        self.assertEqual("unresolved", reservation.state)
        self.assertAlmostEqual(projected, guard.run_spend_usd, places=9)

    def test_release_refunds_a_request_that_produced_no_tokens(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard)
        guard.release(reservation)
        self.assertEqual("released", reservation.state)
        self.assertEqual(0.0, guard.run_spend_usd)
        self.assertEqual(0.0, guard.rolling_spend_usd)

    def test_released_reservations_do_not_change_budget_checks(self) -> None:
        guard = self.make_guard()
        reservation = self.authorize(guard, prompt_tokens=500_000)
        guard.release(reservation)
        self.authorize(guard, prompt_tokens=500_000)

    def test_recorder_observes_reservation_and_resolution(self) -> None:
        events: list[tuple[str, str]] = []

        def recorder(event, reservation, usage):
            events.append((event, reservation.state))

        guard = self.make_guard(recorder=recorder)
        reservation = self.authorize(guard)
        guard.reconcile(reservation, price_probe_usage(0.00001))
        self.assertEqual([("reservation", "reserved"), ("reconcile", "consumed")], events)

    def test_invalid_budgets_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CostGuard(run_budget_usd=0.0, rolling_budget_usd=7.5)
        with self.assertRaises(ValueError):
            CostGuard(run_budget_usd=0.2, rolling_budget_usd=0.0)


if __name__ == "__main__":
    unittest.main()
