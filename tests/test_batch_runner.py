from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from _support import ARTICLE, SRC  # noqa: F401
from daily_chip_news.app import run_daily
from daily_chip_news.config import Settings
from daily_chip_news.errors import classify_failure, health_event_for
from daily_chip_news.gemini import GeminiAPIError, GeminiResponseError
from daily_chip_news.graph import NodeExecutionError
from daily_chip_news.health import HealthEvent, ServiceHealth
from daily_chip_news.publisher import PublisherError
from daily_chip_news.schemas import SchemaError
from daily_chip_news.sources import (
    SourceCollectionError,
    SourceCollectionResult,
    SourceError,
    SourceFailure,
)


def settings(**overrides) -> Settings:
    values = {
        "gemini_api_key": "test-key",
        "writer_model": "writer-model",
        "reviewer_model": "review-model",
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "test-chat",
        "articles_per_feed": 1,
        "max_revisions": 1,
    }
    values.update(overrides)
    return Settings(**values)


def article(name: str):
    return {**ARTICLE, "title": name, "url": f"https://example.com/{name.lower()}"}


def articles(count: int):
    return [article(f"Article {index}") for index in range(count)]


def passed(revisions: int = 0):
    return {
        "status": "PASS",
        "published": True,
        "revision_count": revisions,
    }


def skipped():
    return {"status": "SKIP", "published": False, "revision_count": 0}


def service_error(status_code: int = 503):
    return NodeExecutionError(
        "reviewer",
        GeminiAPIError("HTTP error", status_code=status_code, transient=True),
    )


class ScriptedGraph:
    """Scripted articles plus the node-level health events the real nodes record.

    One logical AI call records exactly one event: the Writer call, the Reviewer
    call, and every revision call each get their own event. A failure before the
    Gemini call (source extraction) records nothing.
    """

    def __init__(self, outcomes, health=None):
        self.outcomes = list(outcomes)
        self.calls = []
        self.health = health

    def invoke(self, state):
        self.calls.append(state["article"]["title"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            self._record_failure(outcome)
            raise outcome
        self._record_success(outcome)
        return outcome

    def _success(self) -> None:
        if self.health is not None:
            self.health.record(HealthEvent.SUCCESS)

    def _record_success(self, outcome) -> None:
        self._success()  # Writer call
        if outcome.get("status") == "SKIP":
            return
        self._success()  # Reviewer call
        for _ in range(int(outcome.get("revision_count", 0))):
            self._success()  # revision Writer call
            self._success()  # revision Reviewer call

    def _record_failure(self, exc: Exception) -> None:
        if self.health is None or not isinstance(exc, NodeExecutionError):
            return
        if exc.stage == "reviewer":
            self._success()  # the Writer call succeeded before the Reviewer failed
        elif exc.stage == "publisher":
            self._success()
            self._success()
            return
        info = classify_failure(exc.stage, exc.cause)
        event = health_event_for(exc.stage, info)
        if event is not None:
            self.health.record(event)


class BatchRunnerTests(unittest.TestCase):
    def run_with_output(
        self,
        graph,
        items,
        *,
        alert_publisher=None,
        health=None,
        **settings_kwargs,
    ):
        service_health = health or ServiceHealth(5, 3)
        if isinstance(graph, ScriptedGraph):
            graph.health = service_health
        output = io.StringIO()
        with redirect_stdout(output):
            result = run_daily(
                settings(**settings_kwargs),
                graph=graph,
                articles=items,
                alert_publisher=alert_publisher,
                health=service_health,
            )
        return result, output.getvalue()

    def test_five_successful_articles_are_success_exit_0(self) -> None:
        items = articles(5)
        graph = ScriptedGraph([passed()] * 5)
        result, output = self.run_with_output(graph, items)
        self.assertEqual(5, result["published"])
        self.assertEqual("SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("run_outcome: SUCCESS", output)
        self.assertIn("workflow_status: PASS", output)

    def test_three_successes_and_two_server_errors_are_partial_success_exit_0(
        self,
    ) -> None:
        items = articles(5)
        graph = ScriptedGraph(
            [passed(), passed(), passed(), service_error(), service_error(429)]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(5, result["processed"])
        self.assertEqual(3, result["published"])
        self.assertEqual(2, result["failed"])
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("run_outcome: PARTIAL_SUCCESS", output)
        self.assertIn("workflow_status: PASS", output)
        self.assertIn("TRANSIENT_SERVER: 1", output)
        self.assertIn("TRANSIENT_RATE_LIMIT: 1", output)

    def test_publish_then_breaker_is_partial_success_exit_0(self) -> None:
        items = articles(10)
        graph = ScriptedGraph(
            [
                passed(),
                skipped(),
                NodeExecutionError(
                    "writer",
                    GeminiResponseError("model returned junk"),
                ),
                service_error(),
                service_error(),
                service_error(),
                service_error(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(1, result["published"])
        self.assertEqual(4, result["failed"])
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("breaker_triggered: yes", output)
        self.assertIn("Gemini service breaker OPEN", output)
        self.assertIn("transient=3/5", output)
        self.assertEqual(6, len(graph.calls))

    def test_breaker_waits_for_a_full_window_of_five_outcomes(self) -> None:
        items = articles(6)
        graph = ScriptedGraph(
            [
                service_error(),
                service_error(),
                skipped(),
                passed(),
                passed(),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(6, result["processed"])
        self.assertEqual(2, result["failed"])
        self.assertIn("breaker_triggered: no", output)
        self.assertIn("health_window: window=[S,S,S,S,S] transient=0/5", output)
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])

    def test_schema_errors_do_not_reset_or_count_as_transient(self) -> None:
        items = articles(5)
        schema_error = NodeExecutionError(
            "reviewer", SchemaError("invalid review payload")
        )
        graph = ScriptedGraph(
            [
                service_error(),
                schema_error,
                service_error(),
                schema_error,
                service_error(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(5, result["processed"])
        self.assertEqual(0, result["published"])
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertIn("SCHEMA_INVALID: 2", output)
        # Schema errors occupy window slots without erasing the transient
        # history; per-call granularity then dilutes 3 transients across 10
        # calls, so the breaker stays closed while the run still fails.
        self.assertIn("health_window: window=[T,S,N,S,T] transient=2/5", output)
        self.assertIn("breaker_triggered: no", output)

    def test_writer_side_transient_burst_opens_the_breaker(self) -> None:
        items = articles(6)
        writer_error = NodeExecutionError(
            "writer",
            GeminiAPIError("HTTP 503", status_code=503, transient=True),
        )
        graph = ScriptedGraph([writer_error] * 5 + [passed()])
        result, output = self.run_with_output(graph, items)
        self.assertEqual(5, result["processed"])
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertEqual(1, result["exit_code"])
        self.assertIn("Gemini service breaker OPEN", output)
        self.assertIn("window=[T,T,T,T,T] transient=5/5 threshold=3", output)

    def test_two_transient_failures_never_open_the_breaker(self) -> None:
        items = articles(6)
        graph = ScriptedGraph(
            [service_error(), service_error(), passed(), passed(), passed(), passed()]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(6, result["processed"])
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertIn("breaker_triggered: no", output)

    def test_no_candidates_is_empty_success_exit_0(self) -> None:
        graph = ScriptedGraph([])
        result, output = self.run_with_output(graph, [])
        self.assertEqual("EMPTY_SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("run_outcome: EMPTY_SUCCESS", output)

    def test_all_candidates_skipped_is_empty_success_exit_0(self) -> None:
        items = articles(2)
        graph = ScriptedGraph([skipped(), skipped()])
        result, output = self.run_with_output(graph, items)
        self.assertEqual(0, result["published"])
        self.assertEqual(0, result["failed"])
        self.assertEqual("EMPTY_SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])

    def test_all_hold_without_api_failures_is_empty_success(self) -> None:
        items = articles(2)
        graph = ScriptedGraph(
            [
                {"status": "HOLD", "published": False, "revision_count": 1},
                {"status": "HOLD", "published": False, "revision_count": 1},
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual("EMPTY_SUCCESS", result["run_outcome"])
        self.assertIn("held: 2", output)

    def test_all_articles_failing_on_gemini_is_failed_exit_1(self) -> None:
        items = articles(2)
        graph = ScriptedGraph([service_error(), service_error()])
        alerts = []
        result, output = self.run_with_output(
            graph, items, alert_publisher=alerts.append
        )
        self.assertEqual(0, result["published"])
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertEqual(1, result["exit_code"])
        self.assertIn("workflow_status: FAIL", output)
        self.assertEqual(1, len(alerts))
        self.assertIn("Outcome: FAILED", alerts[0])
        self.assertIn("TRANSIENT_SERVER=2", alerts[0])

    def test_single_transient_failure_is_item_scoped(self) -> None:
        items = articles(3)
        graph = ScriptedGraph([service_error(), passed(), skipped()])
        result, output = self.run_with_output(graph, items)
        self.assertEqual(3, result["processed"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertIn("status_code=503", output)
        self.assertNotIn("breaker_triggered: yes", output)

    def test_article_extraction_failure_is_item_scoped(self) -> None:
        items = articles(2)
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "writer", SourceError("source body unavailable")
                ),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertIn("SOURCE_ERROR: 1", output)
        self.assertNotIn("source body unavailable", output)

    def test_schema_failure_is_item_scoped(self) -> None:
        items = articles(2)
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "reviewer", SchemaError("invalid review payload")
                ),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertIn("SCHEMA_INVALID: 1", output)
        self.assertNotIn("invalid review payload", output)

    def test_configuration_error_after_publishing_stays_partial_success(self) -> None:
        items = articles(2)
        graph = ScriptedGraph(
            [
                passed(),
                NodeExecutionError(
                    "writer",
                    GeminiAPIError("HTTP 401", status_code=401, global_failure=True),
                ),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(["Article 0", "Article 1"], graph.calls)
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("CONFIG_ERROR: 1", output)

    def test_configuration_error_without_publication_is_failed(self) -> None:
        items = articles(2)
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "writer",
                    GeminiAPIError("HTTP 403", status_code=403, global_failure=True),
                ),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(["Article 0"], graph.calls)
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertEqual(1, result["exit_code"])

    def test_unexpected_program_error_is_failed_even_after_publishing(self) -> None:
        items = articles(2)
        graph = ScriptedGraph([passed(), RuntimeError("bug")])
        result, output = self.run_with_output(graph, items)
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertEqual(1, result["exit_code"])
        self.assertIn("UNEXPECTED_ERROR: 1", output)

    def test_invalid_terminal_state_is_failed(self) -> None:
        items = articles(1)
        graph = ScriptedGraph([{"status": "DRAFTED", "published": False}])
        result, output = self.run_with_output(graph, items)
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertIn("invalid-terminal-state", output)

    def test_publisher_transient_failure_does_not_open_gemini_breaker(self) -> None:
        items = articles(3)
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "publisher",
                    PublisherError("HTTP 500", status_code=500, transient=True),
                ),
                passed(),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(2, result["published"])
        self.assertEqual(1, result["failed"])
        self.assertIn("breaker_triggered: no", output)
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])

    def test_partial_source_failure_is_reported_without_failing_run(self) -> None:
        collection = SourceCollectionResult(
            articles=[article("Article A")],
            sources_total=5,
            sources_ok=4,
            sources_failed=1,
            failures=[SourceFailure("https://failed.example/rss", "TimeoutError")],
        )
        graph = ScriptedGraph([passed()])
        output = io.StringIO()

        with patch("daily_chip_news.app.collect_articles", return_value=collection):
            with redirect_stdout(output):
                result = run_daily(settings(), graph=graph)

        self.assertEqual(4, result["sources_ok"])
        self.assertEqual(1, result["sources_failed"])
        self.assertEqual("SUCCESS", result["run_outcome"])
        self.assertIn("sources_total: 5", output.getvalue())

    def test_all_source_failures_are_failed_without_processing(self) -> None:
        collection = SourceCollectionResult(
            articles=[],
            sources_total=5,
            sources_ok=0,
            sources_failed=5,
            failures=[
                SourceFailure(f"https://failed-{index}.example/rss", "ValueError")
                for index in range(5)
            ],
        )
        graph = ScriptedGraph([])
        alerts = []
        output = io.StringIO()

        with patch(
            "daily_chip_news.app.collect_articles",
            side_effect=SourceCollectionError(collection),
        ):
            with redirect_stdout(output):
                result = run_daily(
                    settings(), graph=graph, alert_publisher=alerts.append
                )

        self.assertEqual([], graph.calls)
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertEqual(1, result["exit_code"])
        self.assertIn("sources_failed: 5", output.getvalue())
        self.assertEqual(1, len(alerts))
        self.assertIn("SourceCollectionError", alerts[0])

    def test_partial_success_sends_a_labelled_warning_alert(self) -> None:
        items = articles(2)
        graph = ScriptedGraph([passed(), service_error()])
        alerts = []
        result, _ = self.run_with_output(graph, items, alert_publisher=alerts.append)
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertEqual(1, len(alerts))
        self.assertIn("部分成功", alerts[0])
        self.assertIn("Published: 1/2", alerts[0])

    def test_success_sends_no_alert(self) -> None:
        items = articles(1)
        graph = ScriptedGraph([passed()])
        alerts = []
        self.run_with_output(graph, items, alert_publisher=alerts.append)
        self.assertEqual([], alerts)

    def test_run_budget_stops_processing_without_marking_success_as_failed(self) -> None:
        items = articles(3)
        graph = ScriptedGraph([passed(), passed(), passed()])
        times = iter([0.0, 0.0, 0.0, 400.0, 400.0])
        output = io.StringIO()
        with redirect_stdout(output):
            result = run_daily(
                settings(run_budget_seconds=300.0),
                graph=graph,
                articles=items,
                clock=lambda: next(times, 400.0),
            )
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, result["published"])
        self.assertEqual("PARTIAL_SUCCESS", result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("Run budget reached", output.getvalue())

    def test_summary_reports_grouped_sections(self) -> None:
        items = articles(3)
        graph = ScriptedGraph([passed(), skipped(), service_error()])
        _, output = self.run_with_output(graph, items)
        for section in ("Sources:", "Articles:", "Gemini:", "Writer:", "Reviewer:"):
            self.assertIn(section, output)
        for field in (
            "transient_failures:",
            "rate_limit_429:",
            "server_5xx:",
            "network:",
            "response_invalid:",
            "retries:",
            "breaker_triggered:",
            "health_window:",
            "failure_categories:",
        ):
            self.assertIn(field, output)


if __name__ == "__main__":
    unittest.main()
