from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC, make_settings  # noqa: F401

from daily_chip_news.gemini import GeminiAPIError
from daily_chip_news.graph import (
    STATUS_HOLD,
    STATUS_PUBLISHED,
    STATUS_REJECT,
    STATUS_SKIP,
    NodeExecutionError,
)
from daily_chip_news.health import HealthEvent, ServiceHealth
from daily_chip_news.outcomes import RunOutcome
from daily_chip_news.runner import run_daily
from daily_chip_news.sources import (
    SourceCollectionError,
    SourceCollectionResult,
    SourceFailure,
    candidate_id,
)


def candidates(count: int) -> list[dict]:
    items = []
    for index in range(count):
        url = f"https://example.com/story-{index}"
        items.append(
            {
                "id": candidate_id(url),
                "title": f"Story {index}",
                "url": url,
                "source": f"Feed {index}",
                "published_at": "",
                "metadata": {},
            }
        )
    return items


def terminal(status: str, *, revisions: int = 0) -> dict:
    return {"status": status, "revision_count": revisions, "published": True}


class FakeGraph:
    """Replay terminal states or raise scripted node failures."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.states: list[dict] = []

    def invoke(self, state: dict) -> dict:
        self.states.append(dict(state))
        if not self.results:
            raise AssertionError("No fake graph result configured")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def clock_from(values: list[float], fallback: float = 1e9):
    iterator = iter(values)
    return lambda: next(iterator, fallback)


class SuccessPathTests(unittest.TestCase):
    def test_all_candidates_published_is_success(self) -> None:
        graph = FakeGraph(terminal(STATUS_PUBLISHED), terminal(STATUS_PUBLISHED))
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(2),
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.SUCCESS.value, result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(2, result["published"])
        self.assertEqual(0, result["failed"])
        self.assertEqual(0, result["skipped"])

    def test_candidate_is_handed_to_the_graph_with_fresh_revision_state(self) -> None:
        graph = FakeGraph(terminal(STATUS_PUBLISHED))
        run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(1),
            clock=lambda: 0.0,
        )
        state = graph.states[0]
        self.assertEqual(candidates(1)[0]["id"], state["candidate"]["id"])
        self.assertEqual(0, state["revision_count"])
        self.assertEqual([], state["revision_brief"])
        self.assertEqual("NEW", state["status"])
        self.assertFalse(state["published"])

    def test_revision_count_is_aggregated(self) -> None:
        graph = FakeGraph(terminal(STATUS_PUBLISHED, revisions=1))
        result = run_daily(
            make_settings(), graph=graph, candidates=candidates(1), clock=lambda: 0.0
        )
        self.assertEqual(1, result["revisions"])

    def test_skips_are_counted_by_reason(self) -> None:
        graph = FakeGraph(
            terminal(STATUS_SKIP),
            terminal(STATUS_REJECT),
            terminal(STATUS_HOLD, revisions=1),
        )
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(3),
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.SUCCESS.value, result["run_outcome"])
        self.assertEqual(0, result["published"])
        self.assertEqual(3, result["skipped"])
        self.assertEqual(1, result["skipped_not_relevant"])
        self.assertEqual(1, result["skipped_rejected"])
        self.assertEqual(1, result["skipped_revision_limit"])

    def test_no_alerts_are_sent_for_a_clean_run(self) -> None:
        alerts: list[str] = []
        run_daily(
            make_settings(),
            graph=FakeGraph(terminal(STATUS_PUBLISHED)),
            candidates=candidates(1),
            alert_publisher=alerts.append,
            clock=lambda: 0.0,
        )
        self.assertEqual([], alerts)


class FailurePathTests(unittest.TestCase):
    def test_candidate_failure_after_a_publish_is_partial_success(self) -> None:
        graph = FakeGraph(
            terminal(STATUS_PUBLISHED),
            NodeExecutionError(
                "writer", GeminiAPIError("boom", status_code=503, transient=True)
            ),
        )
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(2),
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.PARTIAL_SUCCESS.value, result["run_outcome"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["failed"])

    def test_configuration_failure_stops_the_run(self) -> None:
        graph = FakeGraph(
            NodeExecutionError(
                "researcher", GeminiAPIError("bad key", status_code=403)
            ),
            terminal(STATUS_PUBLISHED),
        )
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(2),
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, result["exit_code"])

    def test_unexpected_exception_is_a_failed_run(self) -> None:
        graph = FakeGraph(RuntimeError("boom"))
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(1),
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])
        self.assertEqual(1, result["failed"])

    def test_invalid_terminal_state_is_a_failed_run(self) -> None:
        graph = FakeGraph({"status": "WEIRD"})
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(1),
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])
        self.assertEqual(1, result["failed"])

    def test_partial_and_failed_runs_send_an_alert(self) -> None:
        alerts: list[str] = []
        run_daily(
            make_settings(),
            graph=FakeGraph(
                NodeExecutionError(
                    "reviewer", GeminiAPIError("boom", status_code=503, transient=True)
                )
            ),
            candidates=candidates(1),
            alert_publisher=alerts.append,
            clock=lambda: 0.0,
        )
        self.assertEqual(1, len(alerts))
        self.assertIn("运行失败", alerts[0])
        self.assertIn("Published: 0/1", alerts[0])

    def test_partial_success_alert_mentions_partial_outcome(self) -> None:
        alerts: list[str] = []
        run_daily(
            make_settings(),
            graph=FakeGraph(
                terminal(STATUS_PUBLISHED),
                NodeExecutionError(
                    "writer", GeminiAPIError("boom", status_code=503, transient=True)
                ),
            ),
            candidates=candidates(2),
            alert_publisher=alerts.append,
            clock=lambda: 0.0,
        )
        self.assertEqual(1, len(alerts))
        self.assertIn("部分成功", alerts[0])

    def test_failed_alert_is_never_masked_by_a_broken_alert_channel(self) -> None:
        def broken_alert(message: str) -> None:
            raise RuntimeError("telegram down")

        result = run_daily(
            make_settings(),
            graph=FakeGraph(RuntimeError("boom")),
            candidates=candidates(1),
            alert_publisher=broken_alert,
            clock=lambda: 0.0,
        )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])


class BreakerTests(unittest.TestCase):
    def test_breaker_stops_further_candidates(self) -> None:
        breaker = ServiceHealth(window_size=2, failure_threshold=1)

        class BrokenGraph:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, state: dict) -> dict:
                self.calls += 1
                breaker.record(HealthEvent.TRANSIENT_FAILURE)
                raise NodeExecutionError(
                    "researcher",
                    GeminiAPIError("boom", status_code=503, transient=True),
                )

        graph = BrokenGraph()
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(5),
            clock=lambda: 0.0,
            breaker=breaker,
        )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])
        self.assertTrue(result["breaker_opened"])
        self.assertLess(graph.calls, 5)
        self.assertEqual(2, graph.calls)

    def test_successes_keep_the_breaker_closed(self) -> None:
        breaker = ServiceHealth(window_size=2, failure_threshold=2)
        graph = FakeGraph(
            terminal(STATUS_PUBLISHED),
            terminal(STATUS_PUBLISHED),
            terminal(STATUS_PUBLISHED),
        )
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(3),
            clock=lambda: 0.0,
            breaker=breaker,
        )
        self.assertFalse(result["breaker_opened"])
        self.assertEqual(3, result["published"])


class BudgetTests(unittest.TestCase):
    def test_run_budget_stops_processing_after_the_deadline(self) -> None:
        graph = FakeGraph(terminal(STATUS_PUBLISHED))
        result = run_daily(
            make_settings(),
            graph=graph,
            candidates=candidates(3),
            # deadline, run start, first loop check, second loop check
            clock=clock_from([0.0, 0.0, 0.0, 1e6]),
        )
        self.assertEqual(RunOutcome.PARTIAL_SUCCESS.value, result["run_outcome"])
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, result["published"])

    def test_budget_stop_without_publishing_is_a_failed_run(self) -> None:
        result = run_daily(
            make_settings(),
            graph=FakeGraph(terminal(STATUS_PUBLISHED)),
            candidates=candidates(2),
            clock=clock_from([0.0, 1e6]),
        )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])


class SourceFailureTests(unittest.TestCase):
    def test_all_sources_unavailable_is_a_failed_run(self) -> None:
        collection = SourceCollectionResult(
            candidates=[],
            sources_total=2,
            sources_ok=0,
            sources_failed=2,
            failures=[SourceFailure("Feed A", "HTTPError")],
        )
        alerts: list[str] = []
        with patch(
            "daily_chip_news.runner.collect_articles",
            side_effect=SourceCollectionError(collection),
        ):
            result = run_daily(
                make_settings(),
                graph=FakeGraph(),
                alert_publisher=alerts.append,
                clock=lambda: 0.0,
            )
        self.assertEqual(RunOutcome.FAILED.value, result["run_outcome"])
        self.assertEqual(0, result["sources_ok"])
        self.assertEqual(2, result["sources_failed"])
        self.assertEqual(1, len(alerts))

    def test_sources_are_collected_through_the_configured_feeds(self) -> None:
        captured: dict[str, object] = {}

        def fake_collect(articles_per_feed: int, *, feeds):
            captured["articles_per_feed"] = articles_per_feed
            captured["feeds"] = tuple(feeds)
            return SourceCollectionResult(
                candidates=candidates(1),
                sources_total=1,
                sources_ok=1,
                sources_failed=0,
                failures=[],
            )

        with patch("daily_chip_news.runner.collect_articles", side_effect=fake_collect):
            result = run_daily(
                make_settings(
                    source_feeds=("https://feed.example/rss",),
                    articles_per_feed=3,
                    max_candidates_per_run=6,
                ),
                graph=FakeGraph(terminal(STATUS_PUBLISHED)),
                clock=lambda: 0.0,
            )
        self.assertEqual(("https://feed.example/rss",), captured["feeds"])
        self.assertEqual(3, captured["articles_per_feed"])
        self.assertEqual(1, result["discovered"])
        self.assertEqual(1, result["selected"])
        self.assertEqual(1, result["published"])


class SummaryFileTests(unittest.TestCase):
    def test_summary_file_records_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "run-summary.json"
            run_daily(
                make_settings(summary_path=str(path)),
                graph=FakeGraph(
                    terminal(STATUS_PUBLISHED),
                    NodeExecutionError(
                        "writer",
                        GeminiAPIError("boom", status_code=503, transient=True),
                    ),
                ),
                candidates=candidates(2),
                clock=lambda: 0.0,
            )
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(RunOutcome.PARTIAL_SUCCESS.value, document["run_outcome"])
        self.assertEqual(1, document["published"])
        self.assertEqual(1, document["failed"])
        self.assertEqual(1, len(document["failed_items"]))
        failure = document["failed_items"][0]
        self.assertEqual("writer", failure["stage"])
        self.assertEqual("TRANSIENT_SERVER", failure["category"])
        self.assertEqual(candidates(2)[1]["id"], failure["candidate_id"])

    def test_summary_file_is_optional(self) -> None:
        result = run_daily(
            make_settings(),
            graph=FakeGraph(terminal(STATUS_PUBLISHED)),
            candidates=candidates(1),
            clock=lambda: 0.0,
        )
        self.assertEqual(1, result["published"])


if __name__ == "__main__":
    unittest.main()
