from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from _support import ARTICLE, SRC  # noqa: F401
from daily_chip_news.app import GlobalWorkflowError, run_daily
from daily_chip_news.config import Settings
from daily_chip_news.gemini import GeminiAPIError, GeminiResponseError
from daily_chip_news.graph import NodeExecutionError
from daily_chip_news.publisher import PublisherError
from daily_chip_news.schemas import SchemaError
from daily_chip_news.sources import (
    SourceCollectionError,
    SourceCollectionResult,
    SourceError,
    SourceFailure,
)


def settings() -> Settings:
    return Settings(
        gemini_api_key="test-key",
        researcher_model="research-model",
        writer_model="writer-model",
        reviewer_model="review-model",
        telegram_bot_token="test-token",
        telegram_chat_id="test-chat",
        articles_per_feed=1,
        max_revisions=2,
    )


def article(name: str):
    return {**ARTICLE, "title": name, "url": f"https://example.com/{name.lower()}"}


def passed(revisions: int = 0):
    return {
        "status": "PASS",
        "published": True,
        "revision_count": revisions,
    }


class ScriptedGraph:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def invoke(self, state):
        self.calls.append(state["article"]["title"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BatchRunnerTests(unittest.TestCase):
    def run_with_output(self, graph, articles):
        output = io.StringIO()
        with redirect_stdout(output):
            result = run_daily(settings(), graph=graph, articles=articles)
        return result, output.getvalue()

    def test_item_failure_does_not_stop_remaining_articles(self) -> None:
        items = [
            article("Article A"),
            article("Article B"),
            article("Article C"),
            article("Article D"),
        ]
        graph = ScriptedGraph(
            [
                passed(),
                NodeExecutionError(
                    "writer", GeminiResponseError("sensitive model output")
                ),
                {"status": "SKIP", "published": False, "revision_count": 0},
                passed(1),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(4, result["processed"])
        self.assertEqual(2, result["published"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["revisions"])
        self.assertEqual([item["title"] for item in items], graph.calls)
        self.assertIn(
            "Article B | stage=writer | error=GeminiResponseError", output
        )
        self.assertIn("workflow_status: PASS", output)
        self.assertNotIn("sensitive model output", output)

    def test_one_transient_gemini_failure_is_item_scoped(self) -> None:
        items = [
            article("Article A"),
            article("Article B"),
            article("Article C"),
        ]
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "researcher", GeminiAPIError("HTTP 503", transient=True)
                ),
                passed(),
                {"status": "SKIP", "published": False, "revision_count": 0},
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(3, result["processed"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["skipped"])
        self.assertIn("workflow_status: PASS", output)

    def test_article_extraction_failure_is_item_scoped(self) -> None:
        items = [article("Article A"), article("Article B")]
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "researcher", SourceError("source body unavailable")
                ),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(2, result["processed"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertIn("error=SourceError", output)
        self.assertNotIn("source body unavailable", output)

    def test_article_schema_failure_is_item_scoped(self) -> None:
        items = [article("Article A"), article("Article B")]
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "reviewer", SchemaError("invalid review payload")
                ),
                passed(),
            ]
        )

        result, output = self.run_with_output(graph, items)

        self.assertEqual(2, result["processed"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertIn("error=SchemaError", output)
        self.assertNotIn("invalid review payload", output)

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
        self.assertEqual(1, result["published"])
        self.assertIn("sources_total: 5", output.getvalue())
        self.assertIn("workflow_status: PASS", output.getvalue())

    def test_all_source_failures_fail_run_after_summary(self) -> None:
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
        output = io.StringIO()

        with patch(
            "daily_chip_news.app.collect_articles",
            side_effect=SourceCollectionError(collection),
        ):
            with redirect_stdout(output):
                with self.assertRaises(GlobalWorkflowError) as context:
                    run_daily(settings(), graph=graph)

        self.assertEqual("sources", context.exception.stage)
        self.assertEqual([], graph.calls)
        self.assertIn("sources_failed: 5", output.getvalue())
        self.assertIn("workflow_status: FAIL", output.getvalue())

    def test_consecutive_transient_gemini_failures_trip_global_circuit(self) -> None:
        items = [
            article("Article A"),
            article("Article B"),
            article("Article C"),
        ]

        def failure():
            return NodeExecutionError(
                "researcher", GeminiAPIError("HTTP 503", transient=True)
            )

        graph = ScriptedGraph([failure(), failure(), passed()])
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(GlobalWorkflowError) as context:
                run_daily(settings(), graph=graph, articles=items)
        self.assertEqual("GeminiServiceUnavailable", context.exception.error_type)
        self.assertEqual(["Article A", "Article B"], graph.calls)
        self.assertIn("processed: 2", output.getvalue())
        self.assertIn("workflow_status: FAIL", output.getvalue())

    def test_publisher_stage_resets_gemini_transient_circuit(self) -> None:
        items = [
            article("Article A"),
            article("Article B"),
            article("Article C"),
            article("Article D"),
        ]
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "researcher", GeminiAPIError("HTTP 503", transient=True)
                ),
                NodeExecutionError(
                    "publisher", PublisherError("HTTP 500", transient=True)
                ),
                NodeExecutionError(
                    "researcher", GeminiAPIError("HTTP 503", transient=True)
                ),
                passed(),
            ]
        )
        result, output = self.run_with_output(graph, items)
        self.assertEqual(4, result["processed"])
        self.assertEqual(3, result["failed"])
        self.assertEqual(1, result["published"])
        self.assertIn("workflow_status: PASS", output)

    def test_authentication_failure_stops_run_after_summary(self) -> None:
        items = [
            article("Article A"),
            article("Article B"),
            article("Article C"),
        ]
        auth_error = NodeExecutionError(
            "researcher",
            GeminiAPIError("HTTP 401", status_code=401, global_failure=True),
        )
        graph = ScriptedGraph([passed(), auth_error, passed()])
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(GlobalWorkflowError) as context:
                run_daily(settings(), graph=graph, articles=items)
        self.assertEqual("GeminiAPIError", context.exception.error_type)
        self.assertEqual(["Article A", "Article B"], graph.calls)
        self.assertIn("published: 1", output.getvalue())
        self.assertIn("failed: 1", output.getvalue())
        self.assertIn("workflow_status: FAIL", output.getvalue())

    def test_telegram_configuration_failure_is_global(self) -> None:
        items = [article("Article A"), article("Article B")]
        graph = ScriptedGraph(
            [
                NodeExecutionError(
                    "publisher",
                    PublisherError(
                        "HTTP 401", status_code=401, global_failure=True
                    ),
                ),
                passed(),
            ]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(GlobalWorkflowError) as context:
                run_daily(settings(), graph=graph, articles=items)
        self.assertEqual("publisher", context.exception.stage)
        self.assertEqual(["Article A"], graph.calls)
        self.assertIn("workflow_status: FAIL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
