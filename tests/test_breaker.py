from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401

from daily_chip_news.errors import (
    FailureCategory,
    classify_failure,
    health_event_for,
    record_health_outcome,
)
from daily_chip_news.gemini import (
    GeminiAPIError,
    GeminiResponseError,
    GeminiTruncatedResponseError,
)
from daily_chip_news.health import HealthEvent, ServiceHealth
from daily_chip_news.publisher import PublisherError
from daily_chip_news.schemas import SchemaError
from daily_chip_news.sources import SourceError


class HealthWindowTests(unittest.TestCase):
    def test_window_must_hold_the_threshold(self) -> None:
        with self.assertRaises(ValueError):
            ServiceHealth(window_size=0, failure_threshold=1)
        with self.assertRaises(ValueError):
            ServiceHealth(window_size=3, failure_threshold=4)

    def test_breaker_stays_closed_below_the_threshold(self) -> None:
        breaker = ServiceHealth(window_size=5, failure_threshold=3)
        for _ in range(2):
            breaker.record(HealthEvent.TRANSIENT_FAILURE)
        self.assertFalse(breaker.is_open)

    def test_breaker_opens_once_the_window_fills_with_transient_failures(self) -> None:
        breaker = ServiceHealth(window_size=5, failure_threshold=3)
        for _ in range(3):
            breaker.record(HealthEvent.TRANSIENT_FAILURE)
        # The window must fill before the breaker can open, so a short burst of
        # failures inside an incomplete window never stops the run prematurely.
        self.assertFalse(breaker.is_open)
        breaker.record(HealthEvent.TRANSIENT_FAILURE)
        breaker.record(HealthEvent.SUCCESS)
        self.assertTrue(breaker.is_open)
        self.assertEqual(4, breaker.transient_count)
        self.assertIn("transient=4/5", breaker.snapshot())

    def test_window_must_fill_before_opening(self) -> None:
        breaker = ServiceHealth(window_size=3, failure_threshold=3)
        for _ in range(2):
            breaker.record(HealthEvent.TRANSIENT_FAILURE)
        self.assertFalse(breaker.is_open)
        breaker.record(HealthEvent.TRANSIENT_FAILURE)
        self.assertTrue(breaker.is_open)

    def test_non_transient_failures_do_not_count(self) -> None:
        breaker = ServiceHealth(window_size=4, failure_threshold=2)
        breaker.record(HealthEvent.NON_TRANSIENT_FAILURE)
        breaker.record(HealthEvent.NON_TRANSIENT_FAILURE)
        breaker.record(HealthEvent.TRANSIENT_FAILURE)
        self.assertFalse(breaker.is_open)

    def test_successes_push_failures_out_of_the_window(self) -> None:
        breaker = ServiceHealth(window_size=3, failure_threshold=3)
        breaker.record(HealthEvent.TRANSIENT_FAILURE)
        breaker.record(HealthEvent.TRANSIENT_FAILURE)
        breaker.record(HealthEvent.SUCCESS)
        breaker.record(HealthEvent.SUCCESS)
        self.assertFalse(breaker.is_open)
        self.assertEqual(1, breaker.transient_count)


class FailureClassificationTests(unittest.TestCase):
    def test_transient_service_categories(self) -> None:
        rate_limit = classify_failure(
            "researcher", GeminiAPIError("x", status_code=429, transient=True)
        )
        self.assertIs(FailureCategory.TRANSIENT_RATE_LIMIT, rate_limit.category)
        self.assertTrue(rate_limit.transient)

        server = classify_failure(
            "writer", GeminiAPIError("x", status_code=503, transient=True)
        )
        self.assertIs(FailureCategory.TRANSIENT_SERVER, server.category)

        network = classify_failure("reviewer", GeminiAPIError("x", transient=True))
        self.assertIs(FailureCategory.TRANSIENT_NETWORK, network.category)

    def test_configuration_failure_stops_the_run(self) -> None:
        info = classify_failure(
            "researcher", GeminiAPIError("x", status_code=403, global_failure=True)
        )
        self.assertIs(FailureCategory.CONFIG_ERROR, info.category)
        self.assertTrue(info.stop_run)
        self.assertFalse(info.force_failed)

    def test_model_response_failures_are_candidate_level(self) -> None:
        for cause, category in (
            (GeminiResponseError("x"), FailureCategory.MODEL_RESPONSE_INVALID),
            (
                GeminiTruncatedResponseError("x"),
                FailureCategory.MODEL_RESPONSE_TRUNCATED,
            ),
            (SchemaError("x"), FailureCategory.SCHEMA_INVALID),
            (SourceError("x"), FailureCategory.SOURCE_ERROR),
        ):
            info = classify_failure("researcher", cause)
            self.assertIs(category, info.category)
            self.assertFalse(info.stop_run)

    def test_publisher_failures_keep_their_scope(self) -> None:
        global_failure = classify_failure(
            "publisher", PublisherError("x", status_code=401, global_failure=True)
        )
        self.assertIs(FailureCategory.PUBLISH_ERROR, global_failure.category)
        self.assertTrue(global_failure.stop_run)

        transient = classify_failure(
            "publisher", PublisherError("x", status_code=429, transient=True)
        )
        self.assertTrue(transient.transient)
        self.assertFalse(transient.stop_run)

    def test_unexpected_errors_force_a_failed_run(self) -> None:
        info = classify_failure("researcher", RuntimeError("boom"))
        self.assertIs(FailureCategory.UNEXPECTED_ERROR, info.category)
        self.assertTrue(info.stop_run)
        self.assertTrue(info.force_failed)


class HealthEventMappingTests(unittest.TestCase):
    def test_breaker_events_cover_gemini_stages_only(self) -> None:
        transient = classify_failure(
            "writer", GeminiAPIError("x", status_code=503, transient=True)
        )
        self.assertIs(
            HealthEvent.TRANSIENT_FAILURE, health_event_for("writer", transient)
        )
        invalid = classify_failure("writer", SchemaError("x"))
        self.assertIs(
            HealthEvent.NON_TRANSIENT_FAILURE, health_event_for("writer", invalid)
        )
        self.assertIsNone(health_event_for("publisher", transient))

    def test_source_errors_do_not_touch_the_breaker(self) -> None:
        events: list[HealthEvent] = []
        record_health_outcome(
            events.append, stage="researcher", cause=SourceError("no body")
        )
        self.assertEqual([], events)

    def test_successful_call_records_success(self) -> None:
        events: list[HealthEvent] = []
        record_health_outcome(events.append, stage="reviewer")
        self.assertEqual([HealthEvent.SUCCESS], events)

    def test_recorder_is_optional(self) -> None:
        record_health_outcome(None, stage="writer")


if __name__ == "__main__":
    unittest.main()
