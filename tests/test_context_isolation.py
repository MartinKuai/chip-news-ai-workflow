from __future__ import annotations

import json
import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, CaptureClient, research_output, review  # noqa: F401
from daily_chip_news.gemini import GeminiAPIError, GeminiResponseError
from daily_chip_news.health import HealthEvent
from daily_chip_news.nodes import ResearcherNode, ReviewerNode, WriterNode
from daily_chip_news.schemas import SchemaError


class ExplodingClient:
    """Client stub that raises one configured exception on every call."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def request_json(self, **kwargs):
        raise self._exc


class ContextBoundaryTests(unittest.TestCase):
    def test_researcher_is_the_only_node_that_sees_the_raw_article(self) -> None:
        client = CaptureClient(research_output(), DRAFT, review("PASS"))
        researcher = ResearcherNode(
            client, "researcher-model", lambda url: "Body line.\n" * 30
        )
        research_update = researcher({"article": ARTICLE})
        self.assertIn("raw_content", client.calls[0]["payload"])

        writer = WriterNode(client, "writer-model")
        draft_update = writer({"article": ARTICLE, **research_update})
        writer_payload = client.calls[1]["payload"]
        self.assertEqual(
            {
                "editorial_brief",
                "research_notes",
                "revision_brief",
                "output_schema",
            },
            set(writer_payload),
        )
        self.assertNotIn("raw_content", json.dumps(writer_payload))

        ReviewerNode(client, "review-model")(
            {"article": ARTICLE, **research_update, **draft_update}
        )
        reviewer_payload = client.calls[2]["payload"]
        self.assertEqual(
            {"research_notes", "draft", "rubric", "output_schema"},
            set(reviewer_payload),
        )
        self.assertNotIn("raw_content", json.dumps(reviewer_payload))

    def test_writer_compose_payload_has_no_previous_draft_or_raw_text(self) -> None:
        client = CaptureClient(DRAFT)
        WriterNode(client, "writer-model")(
            {
                "article": ARTICLE,
                "research_notes": RESEARCH,
                "revision_brief": [],
                "raw_content": "must not leak",
                "raw_article": "must not leak",
                "review": {"must": "not leak"},
            }
        )
        payload = client.calls[0]["payload"]
        self.assertNotIn("previous_draft", payload)
        serialized = json.dumps(payload)
        self.assertNotIn("must not leak", serialized)
        for forbidden in ("raw_content", "raw_article", "reviewer"):
            self.assertNotIn(forbidden, serialized)

    def test_writer_revision_uses_previous_draft_and_keeps_notes(self) -> None:
        client = CaptureClient(DRAFT)
        writer = WriterNode(client, "writer-model")
        writer(
            {
                "article": ARTICLE,
                "research_notes": RESEARCH,
                "draft": DRAFT,
                "revision_brief": ["缩短标题"],
                "raw_content": "must not leak",
            }
        )
        payload = client.calls[0]["payload"]
        self.assertEqual(
            {
                "editorial_brief",
                "research_notes",
                "revision_brief",
                "output_schema",
                "previous_draft",
            },
            set(payload),
        )
        self.assertEqual(DRAFT, payload["previous_draft"])
        self.assertEqual(RESEARCH, payload["research_notes"])
        self.assertNotIn("raw_content", json.dumps(payload))

    def test_writer_does_not_change_research_notes(self) -> None:
        client = CaptureClient(DRAFT)
        update = WriterNode(client, "writer-model")(
            {
                "article": ARTICLE,
                "research_notes": RESEARCH,
                "draft": DRAFT,
                "revision_brief": ["缩短标题"],
            }
        )
        self.assertNotIn("research_notes", update)
        self.assertEqual(DRAFT, update["draft"])

    def test_three_nodes_route_to_three_models(self) -> None:
        client = CaptureClient(research_output(), DRAFT, review("PASS"))
        research_update = ResearcherNode(
            client, "researcher-model", lambda url: "x" * 300
        )({"article": ARTICLE})
        draft_update = WriterNode(client, "writer-model")(
            {"article": ARTICLE, **research_update}
        )
        ReviewerNode(client, "review-model")(
            {"article": ARTICLE, **research_update, **draft_update}
        )
        self.assertEqual(
            ["researcher-model", "writer-model", "review-model"],
            [call["model"] for call in client.calls],
        )

    def test_node_generation_settings_differ_by_role(self) -> None:
        client = CaptureClient(research_output(), DRAFT, review("PASS"))
        research_update = ResearcherNode(
            client,
            "researcher-model",
            lambda url: "x" * 300,
            thinking_level="low",
            max_output_tokens=3072,
        )({"article": ARTICLE})
        draft_update = WriterNode(
            client,
            "writer-model",
            thinking_level="low",
            max_output_tokens=2560,
        )({"article": ARTICLE, **research_update})
        ReviewerNode(
            client,
            "review-model",
            thinking_level="low",
            max_output_tokens=1024,
        )({"article": ARTICLE, **research_update, **draft_update})
        self.assertEqual(
            [("researcher", 3072), ("writer", 2560), ("reviewer", 1024)],
            [(call["purpose"], call["max_output_tokens"]) for call in client.calls],
        )

    def test_reviewer_cannot_pass_below_qa_threshold(self) -> None:
        weak_pass = review("PASS")
        weak_pass["scores"]["factuality"] = 7
        client = CaptureClient(weak_pass)
        with self.assertRaises(SchemaError):
            ReviewerNode(client, "review-model")(
                {"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT}
            )


class HealthRecordingTests(unittest.TestCase):
    """One logical AI call must produce exactly one health event."""

    def test_writer_success_records_one_event(self) -> None:
        events = []
        WriterNode(
            CaptureClient(DRAFT),
            "writer-model",
            health_recorder=events.append,
        )({"article": ARTICLE, "research_notes": RESEARCH, "revision_brief": []})
        self.assertEqual([HealthEvent.SUCCESS], events)

    def test_reviewer_success_records_one_event(self) -> None:
        events = []
        ReviewerNode(
            CaptureClient(review("PASS")),
            "review-model",
            health_recorder=events.append,
        )({"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT})
        self.assertEqual([HealthEvent.SUCCESS], events)

    def test_writer_transient_exhaustion_records_one_transient_event(self) -> None:
        events = []
        node = WriterNode(
            ExplodingClient(
                GeminiAPIError("HTTP 503", status_code=503, transient=True)
            ),
            "writer-model",
            health_recorder=events.append,
        )
        with self.assertRaises(GeminiAPIError):
            node({"article": ARTICLE, "research_notes": RESEARCH, "revision_brief": []})
        self.assertEqual([HealthEvent.TRANSIENT_FAILURE], events)

    def test_reviewer_transient_exhaustion_records_one_transient_event(self) -> None:
        events = []
        node = ReviewerNode(
            ExplodingClient(
                GeminiAPIError("HTTP 429", status_code=429, transient=True)
            ),
            "review-model",
            health_recorder=events.append,
        )
        with self.assertRaises(GeminiAPIError):
            node({"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT})
        self.assertEqual([HealthEvent.TRANSIENT_FAILURE], events)

    def test_writer_response_error_records_one_neutral_event(self) -> None:
        events = []
        node = WriterNode(
            ExplodingClient(GeminiResponseError("invalid json")),
            "writer-model",
            health_recorder=events.append,
        )
        with self.assertRaises(GeminiResponseError):
            node({"article": ARTICLE, "research_notes": RESEARCH, "revision_brief": []})
        self.assertEqual([HealthEvent.NON_TRANSIENT_FAILURE], events)

    def test_schema_failure_records_exactly_one_neutral_event(self) -> None:
        events = []
        broken = dict(DRAFT)
        broken["headline"] = ""
        node = WriterNode(
            CaptureClient(broken),
            "writer-model",
            health_recorder=events.append,
        )
        with self.assertRaises(SchemaError):
            node({"article": ARTICLE, "research_notes": RESEARCH, "revision_brief": []})
        self.assertEqual([HealthEvent.NON_TRANSIENT_FAILURE], events)

    def test_configuration_error_records_no_health_event(self) -> None:
        events = []
        node = ReviewerNode(
            ExplodingClient(
                GeminiAPIError("HTTP 401", status_code=401, global_failure=True)
            ),
            "review-model",
            health_recorder=events.append,
        )
        with self.assertRaises(GeminiAPIError):
            node({"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT})
        self.assertEqual([], events)


if __name__ == "__main__":
    unittest.main()
