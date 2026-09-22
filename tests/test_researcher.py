from __future__ import annotations

import unittest

from _support import CANDIDATE, SRC, CaptureClient, research_output  # noqa: F401

from daily_chip_news.config import NodeProfile
from daily_chip_news.errors import record_health_outcome
from daily_chip_news.gemini import GeminiAPIError
from daily_chip_news.health import HealthEvent, ServiceHealth
from daily_chip_news.nodes import ResearcherNode
from daily_chip_news.schemas import SchemaError
from daily_chip_news.sources import SourceError

BODY = "Body paragraph about HBM capacity.\n\n" * 40


def extractor(url: str) -> str:
    return BODY


def node(client, **kwargs) -> ResearcherNode:
    profile = kwargs.pop("profile", NodeProfile(model="researcher-model"))
    return ResearcherNode(client, profile, kwargs.pop("extractor", extractor), **kwargs)


def state() -> dict:
    return {"candidate": dict(CANDIDATE), "status": "NEW"}


class ResearcherKeepTests(unittest.TestCase):
    def test_keep_returns_notes_with_deterministic_provenance(self) -> None:
        client = CaptureClient(research_output())
        update = node(client)(state())
        notes = update["research_notes"]
        self.assertEqual("RESEARCHED", update["status"])
        self.assertEqual("KEEP", notes["decision"])
        self.assertEqual(CANDIDATE["source"], notes["source"])
        self.assertEqual(CANDIDATE["url"], notes["url"])
        self.assertEqual(CANDIDATE["published_at"], notes["published_at"])
        self.assertEqual(1, len(notes["notes"]))

    def test_research_adds_entities_numbers_and_gaps(self) -> None:
        client = CaptureClient(
            research_output(
                entities={
                    "companies": ["Vendor"],
                    "products": [],
                    "models": [],
                    "events": [],
                },
                key_numbers=[{"label": "capacity", "value": "50%"}],
                gaps=["台积电未回应"],
            )
        )
        notes = node(client)(state())["research_notes"]
        self.assertEqual(["Vendor"], notes["entities"]["companies"])
        self.assertEqual("50%", notes["key_numbers"][0]["value"])
        self.assertEqual(["台积电未回应"], notes["gaps"])

    def test_optional_research_blocks_degrade_to_empty(self) -> None:
        client = CaptureClient(
            research_output(entities=None, key_numbers=None, gaps=None)
        )
        notes = node(client)(state())["research_notes"]
        self.assertEqual([], notes["entities"]["companies"])
        self.assertEqual([], notes["key_numbers"])
        self.assertEqual([], notes["gaps"])

    def test_request_uses_the_article_body_scope_and_profile(self) -> None:
        client = CaptureClient(research_output())
        profile = NodeProfile(
            model="researcher-model",
            thinking_level="low",
            max_output_tokens=3072,
        )
        node(client, profile=profile)(state())
        call = client.calls[0]
        self.assertEqual("researcher-model", call["model"])
        self.assertEqual("low", call["thinking_level"])
        self.assertEqual(3072, call["max_output_tokens"])
        self.assertEqual("researcher", call["purpose"])
        self.assertEqual(
            CANDIDATE["title"], call["payload"]["article_metadata"]["title"]
        )
        self.assertIn("HBM capacity", call["payload"]["raw_content"])
        self.assertTrue(call["payload"]["editorial_scope"])

    def test_article_body_is_capped_before_the_model_call(self) -> None:
        client = CaptureClient(research_output())
        node(client, max_content_chars=2000)(
            {"candidate": dict(CANDIDATE), "status": "NEW"}
        )
        content = client.calls[0]["payload"]["raw_content"]
        self.assertLessEqual(len(content), 2000 + len("\n[content truncated]"))


class ResearcherSkipTests(unittest.TestCase):
    def test_skip_returns_no_notes(self) -> None:
        client = CaptureClient(
            {
                "decision": "SKIP",
                "reason": "not about our scope",
                "topic": "",
                "notes": [],
            }
        )
        update = node(client)(state())
        self.assertEqual("SKIP", update["status"])
        self.assertEqual("SKIP", update["research_notes"]["decision"])
        self.assertEqual([], update["research_notes"]["notes"])


class ResearcherFailureTests(unittest.TestCase):
    def test_extraction_failure_is_a_source_error_without_a_model_call(self) -> None:
        def broken(url: str) -> str:
            raise SourceError("Article extraction failed with HTTP 404")

        client = CaptureClient()
        with self.assertRaises(SourceError):
            node(client, extractor=broken)(state())
        self.assertEqual([], client.calls)

    def test_empty_extraction_is_a_source_error(self) -> None:
        client = CaptureClient()
        with self.assertRaises(SourceError):
            node(client, extractor=lambda url: "   ")(state())

    def test_malformed_response_is_a_schema_error(self) -> None:
        client = CaptureClient(
            {"decision": "MAYBE", "reason": "x", "topic": "y", "notes": []}
        )
        with self.assertRaises(SchemaError):
            node(client)(state())

    def test_keep_without_notes_is_a_schema_error(self) -> None:
        client = CaptureClient(research_output(notes=[]))
        with self.assertRaises(SchemaError):
            node(client)(state())

    def test_api_failure_propagates_and_updates_the_breaker(self) -> None:
        breaker = ServiceHealth(window_size=2, failure_threshold=1)
        client = CaptureClient(GeminiAPIError("boom", status_code=503, transient=True))
        with self.assertRaises(GeminiAPIError):
            node(client, health_recorder=breaker.record)(state())
        self.assertEqual((HealthEvent.TRANSIENT_FAILURE,), breaker.events)

    def test_successful_call_records_one_breaker_event(self) -> None:
        breaker = ServiceHealth(window_size=3, failure_threshold=2)
        client = CaptureClient(research_output())
        node(client, health_recorder=breaker.record)(state())
        self.assertEqual((HealthEvent.SUCCESS,), breaker.events)

    def test_source_error_is_not_a_service_failure(self) -> None:
        events: list[HealthEvent] = []
        record_health_outcome(
            events.append, stage="researcher", cause=SourceError("no content")
        )
        self.assertEqual([], events)


if __name__ == "__main__":
    unittest.main()
