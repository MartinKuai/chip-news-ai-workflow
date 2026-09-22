from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.cost_guard import CostGuardExceeded, CostGuardStopReason
from daily_chip_news.errors import (
    AI_STAGES,
    FailureCategory,
    classify_failure,
    health_event_for,
)
from daily_chip_news.gemini import (
    GeminiAPIError,
    GeminiResponseError,
    GeminiTruncatedResponseError,
)
from daily_chip_news.health import HealthEvent
from daily_chip_news.publisher import PublisherError
from daily_chip_news.schemas import SchemaError
from daily_chip_news.sources import SourceError


class ClassifyFailureTests(unittest.TestCase):
    def test_rate_limit_is_transient(self) -> None:
        info = classify_failure(
            "reviewer", GeminiAPIError("HTTP 429", status_code=429, transient=True)
        )
        self.assertIs(FailureCategory.TRANSIENT_RATE_LIMIT, info.category)
        self.assertTrue(info.transient)
        self.assertFalse(info.stop_run)
        self.assertIs(HealthEvent.TRANSIENT_FAILURE, health_event_for("reviewer", info))

    def test_server_error_is_transient(self) -> None:
        info = classify_failure(
            "writer", GeminiAPIError("HTTP 503", status_code=503, transient=True)
        )
        self.assertIs(FailureCategory.TRANSIENT_SERVER, info.category)
        self.assertIs(HealthEvent.TRANSIENT_FAILURE, health_event_for("writer", info))

    def test_network_error_is_transient(self) -> None:
        info = classify_failure(
            "writer", GeminiAPIError("network", transient=True)
        )
        self.assertIs(FailureCategory.TRANSIENT_NETWORK, info.category)
        self.assertIs(HealthEvent.TRANSIENT_FAILURE, health_event_for("writer", info))

    def test_configuration_error_stops_run_but_is_not_forced(self) -> None:
        info = classify_failure(
            "writer",
            GeminiAPIError("HTTP 401", status_code=401, global_failure=True),
        )
        self.assertIs(FailureCategory.CONFIG_ERROR, info.category)
        self.assertTrue(info.stop_run)
        self.assertFalse(info.force_failed)

    def test_model_response_failures_occupy_health_window(self) -> None:
        invalid = classify_failure("writer", GeminiResponseError("bad json"))
        self.assertIs(FailureCategory.MODEL_RESPONSE_INVALID, invalid.category)
        self.assertIs(
            HealthEvent.NON_TRANSIENT_FAILURE, health_event_for("writer", invalid)
        )
        truncated = classify_failure("writer", GeminiTruncatedResponseError("cut"))
        self.assertIs(FailureCategory.MODEL_RESPONSE_TRUNCATED, truncated.category)
        self.assertIs(
            HealthEvent.NON_TRANSIENT_FAILURE, health_event_for("writer", truncated)
        )

    def test_schema_failure_is_article_level_without_health_event(self) -> None:
        info = classify_failure("reviewer", SchemaError("bad schema"))
        self.assertIs(FailureCategory.SCHEMA_INVALID, info.category)
        self.assertFalse(info.stop_run)
        self.assertIs(
            HealthEvent.NON_TRANSIENT_FAILURE, health_event_for("reviewer", info)
        )

    def test_source_error_does_not_touch_the_gemini_health_window(self) -> None:
        info = classify_failure("writer", SourceError("extraction failed"))
        self.assertIs(FailureCategory.SOURCE_ERROR, info.category)
        self.assertIsNone(health_event_for("writer", info))

    def test_publisher_error_is_article_level(self) -> None:
        info = classify_failure(
            "publisher", PublisherError("HTTP 500", status_code=500, transient=True)
        )
        self.assertIs(FailureCategory.PUBLISH_ERROR, info.category)
        self.assertIsNone(health_event_for("publisher", info))

    def test_cost_guard_stop_is_expected_and_never_touches_health(self) -> None:
        info = classify_failure(
            "researcher",
            CostGuardExceeded(CostGuardStopReason.RUN_BUDGET, "over budget"),
        )
        self.assertIs(FailureCategory.COST_GUARD, info.category)
        self.assertTrue(info.stop_run)
        self.assertFalse(info.force_failed)
        self.assertFalse(info.transient)
        self.assertIsNone(health_event_for("researcher", info))

    def test_unknown_exception_forces_failed(self) -> None:
        info = classify_failure("graph", TypeError("bug"))
        self.assertIs(FailureCategory.UNEXPECTED_ERROR, info.category)
        self.assertTrue(info.force_failed)

    def test_health_window_only_covers_ai_stages(self) -> None:
        self.assertEqual({"researcher", "writer", "reviewer"}, set(AI_STAGES))


if __name__ == "__main__":
    unittest.main()
