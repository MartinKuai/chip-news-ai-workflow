from __future__ import annotations

import json
import unittest

from _support import ARTICLE, SRC, CaptureClient, research_output  # noqa: F401
from daily_chip_news.gemini import GeminiAPIError, GeminiResponseError
from daily_chip_news.health import HealthEvent
from daily_chip_news.nodes import ResearcherNode
from daily_chip_news.schemas import SchemaError
from daily_chip_news.sources import SourceError


def body_extractor(text: str = "Body line.\n" * 40):
    def extractor(url: str) -> str:
        return text

    return extractor


class ExplodingClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def request_json(self, **kwargs):
        raise self._exc


class ResearcherPayloadTests(unittest.TestCase):
    def test_payload_is_allow_listed_and_cleaned(self) -> None:
        client = CaptureClient(research_output())
        node = ResearcherNode(client, "researcher-model", body_extractor())
        update = node({"article": ARTICLE})
        payload = client.calls[0]["payload"]
        self.assertEqual(
            {
                "article_metadata",
                "raw_content",
                "editorial_scope",
                "output_schema",
            },
            set(payload),
        )
        self.assertEqual("Body line.", payload["raw_content"].splitlines()[0])
        self.assertEqual("RESEARCHED", update["status"])
        serialized = json.dumps(payload)
        self.assertNotIn("raw_article", serialized)

    def test_reader_metadata_is_stripped_before_the_model_call(self) -> None:
        raw = (
            "Title: HBM update\n\n"
            "URL Source: https://example.com/hbm\n\n"
            "Markdown Content:\n"
            "Body line.\n"
        )
        client = CaptureClient(research_output())
        ResearcherNode(client, "researcher-model", body_extractor(raw))(
            {"article": ARTICLE}
        )
        content = client.calls[0]["payload"]["raw_content"]
        self.assertNotIn("Title:", content)
        self.assertNotIn("URL Source:", content)
        self.assertIn("Body line.", content)

    def test_content_is_truncated_by_the_configured_budget(self) -> None:
        client = CaptureClient(research_output())
        ResearcherNode(
            client,
            "researcher-model",
            body_extractor("x" * 5000),
            max_content_chars=2000,
        )({"article": ARTICLE})
        content = client.calls[0]["payload"]["raw_content"]
        self.assertLessEqual(len(content), 2000 + len("\n[content truncated]"))
        self.assertIn("[content truncated]", content)

    def test_generation_settings_are_quality_bounded(self) -> None:
        client = CaptureClient(research_output())
        ResearcherNode(
            client,
            "researcher-model",
            body_extractor(),
            thinking_level="low",
            max_output_tokens=3072,
        )({"article": ARTICLE})
        call = client.calls[0]
        self.assertEqual("researcher", call["purpose"])
        self.assertEqual("low", call["thinking_level"])
        self.assertEqual(3072, call["max_output_tokens"])
        self.assertIsNotNone(call["output_schema"])
        self.assertEqual("researcher-model", call["model"])

    def test_makes_exactly_one_logical_call(self) -> None:
        client = CaptureClient(research_output())
        ResearcherNode(client, "researcher-model", body_extractor())(
            {"article": ARTICLE}
        )
        self.assertEqual(1, len(client.calls))


class ResearcherContractTests(unittest.TestCase):
    def test_provenance_is_injected_deterministically(self) -> None:
        client = CaptureClient(research_output())
        node = ResearcherNode(client, "researcher-model", body_extractor())
        update = node({"article": ARTICLE})
        notes = update["research_notes"]
        self.assertEqual(ARTICLE["source"], notes["source"])
        self.assertEqual(ARTICLE["url"], notes["url"])
        self.assertEqual(ARTICLE["published_at"], notes["published_at"])

    def test_skip_decision_routes_to_skip(self) -> None:
        client = CaptureClient(
            research_output(decision="SKIP", reason="Not in scope", topic="", notes=[])
        )
        update = ResearcherNode(client, "researcher-model", body_extractor())(
            {"article": ARTICLE}
        )
        self.assertEqual("SKIP", update["status"])
        self.assertEqual("SKIP", update["research_notes"]["decision"])

    def test_keep_requires_at_least_one_note(self) -> None:
        client = CaptureClient(research_output(notes=[]))
        with self.assertRaises(SchemaError):
            ResearcherNode(client, "researcher-model", body_extractor())(
                {"article": ARTICLE}
            )

    def test_invalid_decision_is_rejected(self) -> None:
        client = CaptureClient(research_output(decision="MAYBE"))
        with self.assertRaises(SchemaError):
            ResearcherNode(client, "researcher-model", body_extractor())(
                {"article": ARTICLE}
            )

    def test_empty_cleaned_content_is_a_source_error_without_a_gemini_call(self) -> None:
        client = CaptureClient(research_output())
        node = ResearcherNode(client, "researcher-model", body_extractor(""))
        with self.assertRaises(SourceError):
            node({"article": ARTICLE})
        self.assertEqual([], client.calls)


class ResearcherHealthTests(unittest.TestCase):
    def test_success_records_one_event(self) -> None:
        events = []
        ResearcherNode(
            CaptureClient(research_output()),
            "researcher-model",
            body_extractor(),
            health_recorder=events.append,
        )({"article": ARTICLE})
        self.assertEqual([HealthEvent.SUCCESS], events)

    def test_transient_exhaustion_records_one_transient_event(self) -> None:
        events = []
        node = ResearcherNode(
            ExplodingClient(
                GeminiAPIError("HTTP 503", status_code=503, transient=True)
            ),
            "researcher-model",
            body_extractor(),
            health_recorder=events.append,
        )
        with self.assertRaises(GeminiAPIError):
            node({"article": ARTICLE})
        self.assertEqual([HealthEvent.TRANSIENT_FAILURE], events)

    def test_response_error_records_one_neutral_event(self) -> None:
        events = []
        node = ResearcherNode(
            ExplodingClient(GeminiResponseError("invalid json")),
            "researcher-model",
            body_extractor(),
            health_recorder=events.append,
        )
        with self.assertRaises(GeminiResponseError):
            node({"article": ARTICLE})
        self.assertEqual([HealthEvent.NON_TRANSIENT_FAILURE], events)

    def test_schema_failure_records_exactly_one_neutral_event(self) -> None:
        events = []
        node = ResearcherNode(
            CaptureClient(research_output(decision="MAYBE")),
            "researcher-model",
            body_extractor(),
            health_recorder=events.append,
        )
        with self.assertRaises(SchemaError):
            node({"article": ARTICLE})
        self.assertEqual([HealthEvent.NON_TRANSIENT_FAILURE], events)

    def test_source_extraction_failure_records_no_health_event(self) -> None:
        events = []
        node = ResearcherNode(
            CaptureClient(research_output()),
            "researcher-model",
            body_extractor(""),
            health_recorder=events.append,
        )
        with self.assertRaises(SourceError):
            node({"article": ARTICLE})
        self.assertEqual([], events)


if __name__ == "__main__":
    unittest.main()
