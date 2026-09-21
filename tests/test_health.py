from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.health import HealthEvent, ServiceHealth


class ServiceHealthTests(unittest.TestCase):
    def make_health(self, window_size: int = 5, threshold: int = 3) -> ServiceHealth:
        return ServiceHealth(window_size, threshold)

    def record(self, health: ServiceHealth, marks: str) -> None:
        mapping = {
            "S": HealthEvent.SUCCESS,
            "T": HealthEvent.TRANSIENT_FAILURE,
            "N": HealthEvent.NON_TRANSIENT_FAILURE,
        }
        for mark in marks:
            health.record(mapping[mark])

    def test_three_transient_failures_in_five_outcomes_open_breaker(self) -> None:
        health = self.make_health()
        self.record(health, "TTNST")
        self.assertTrue(health.is_open)
        self.assertEqual(3, health.transient_count)

    def test_two_transient_failures_and_three_successes_keep_breaker_closed(self) -> None:
        health = self.make_health()
        self.record(health, "TTSSS")
        self.assertFalse(health.is_open)

    def test_schema_failures_do_not_count_or_reset_history(self) -> None:
        health = self.make_health()
        self.record(health, "TNTNT")
        self.assertTrue(health.is_open)
        self.assertEqual(3, health.transient_count)

    def test_successes_evict_old_transient_failures(self) -> None:
        health = self.make_health()
        self.record(health, "TT")
        self.assertFalse(health.is_open)
        self.record(health, "SSSSS")
        self.assertFalse(health.is_open)
        self.assertEqual(0, health.transient_count)

    def test_window_must_fill_before_breaker_can_open(self) -> None:
        health = self.make_health()
        self.record(health, "TTT")
        self.assertFalse(health.window_full)
        self.assertFalse(health.is_open)

    def test_snapshot_is_log_safe_and_diagnosable(self) -> None:
        health = self.make_health()
        self.record(health, "TTNST")
        self.assertEqual("window=[T,T,N,S,T] transient=3/5 threshold=3", health.snapshot())

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ServiceHealth(window_size=0)
        with self.assertRaises(ValueError):
            ServiceHealth(window_size=5, failure_threshold=0)
        with self.assertRaises(ValueError):
            ServiceHealth(window_size=5, failure_threshold=6)


if __name__ == "__main__":
    unittest.main()
