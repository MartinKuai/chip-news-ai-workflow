from __future__ import annotations

import json
import unittest

from _support import ARTICLE, DRAFT, RESEARCH, SRC, CaptureClient, review, writer_output  # noqa: F401
from daily_chip_news.nodes import ReviewerNode, WriterNode
from daily_chip_news.schemas import SchemaError


def body_extractor(text: str = "Body line.\n" * 40):
    def extractor(url: str) -> str:
        return text

    return extractor


class WriterContextTests(unittest.TestCase):
    def test_compose_payload_is_allow_listed_and_deterministically_cleaned(self) -> None:
        client = CaptureClient(writer_output())
        writer = WriterNode(client, "writer-model", body_extractor())
        update = writer(
            {
                "article": ARTICLE,
                "revision_brief": [],
                "raw_article": "must not leak",
                "raw_content": "must not leak",
                "review": {"must": "not leak"},
            }
        )
        payload = client.calls[0]["payload"]
        self.assertEqual(
            {
                "mode",
                "article_metadata",
                "raw_content",
                "editorial_scope",
                "editorial_brief",
                "output_schema",
            },
            set(payload),
        )
        self.assertEqual("compose", payload["mode"])
        self.assertEqual("Body line.", payload["raw_content"].splitlines()[0])
        self.assertEqual("DRAFTED", update["status"])
        serialized = json.dumps(payload)
        self.assertNotIn("must not leak", serialized)
        for forbidden in ("raw_article", "researcher_prompt"):
            self.assertNotIn(forbidden, serialized)

    def test_writer_strips_reader_metadata_before_the_model_call(self) -> None:
        raw = (
            "Title: HBM update\n\n"
            "URL Source: https://example.com/hbm\n\n"
            "Markdown Content:\n"
            "Body line.\n" * 1
        )
        client = CaptureClient(writer_output())
        WriterNode(client, "writer-model", body_extractor(raw))(
            {"article": ARTICLE, "revision_brief": []}
        )
        content = client.calls[0]["payload"]["raw_content"]
        self.assertNotIn("Title:", content)
        self.assertNotIn("URL Source:", content)
        self.assertIn("Body line.", content)

    def test_revise_payload_reuses_notes_without_refetching_the_source(self) -> None:
        def exploding_extractor(url: str) -> str:
            raise AssertionError("revision must not refetch the source")

        client = CaptureClient(writer_output())
        writer = WriterNode(client, "writer-model", exploding_extractor)
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
                "mode",
                "article_metadata",
                "research_notes",
                "previous_draft",
                "revision_brief",
                "editorial_brief",
                "output_schema",
            },
            set(payload),
        )
        self.assertEqual("revise", payload["mode"])
        self.assertEqual(DRAFT, payload["previous_draft"])
        self.assertNotIn("raw_content", json.dumps(payload))

    def test_revision_cannot_change_the_research_notes(self) -> None:
        altered = writer_output()
        altered["topic"] = "changed topic"
        altered["notes"] = [
            {
                "claim": "A brand new unsupported claim.",
                "evidence": "Invented during revision.",
                "why_it_matters": "Should never replace the original notes.",
                "confidence": 0.4,
            }
        ]
        client = CaptureClient(altered)
        writer = WriterNode(client, "writer-model", body_extractor())
        update = writer(
            {
                "article": ARTICLE,
                "research_notes": RESEARCH,
                "draft": DRAFT,
                "revision_brief": ["缩短标题"],
            }
        )
        self.assertEqual(RESEARCH, update["research_notes"])

    def test_writer_output_decision_skip_routes_to_skip(self) -> None:
        from _support import skip_output

        client = CaptureClient(skip_output())
        update = WriterNode(client, "writer-model", body_extractor())(
            {"article": ARTICLE, "revision_brief": []}
        )
        self.assertEqual("SKIP", update["status"])
        self.assertEqual({}, update["draft"])
        self.assertEqual("SKIP", update["research_notes"]["decision"])

    def test_writer_requests_quality_oriented_generation_settings(self) -> None:
        client = CaptureClient(writer_output())
        WriterNode(
            client,
            "writer-model",
            body_extractor(),
            thinking_level="medium",
            max_output_tokens=16384,
        )({"article": ARTICLE, "revision_brief": []})
        call = client.calls[0]
        self.assertEqual("writer", call["purpose"])
        self.assertEqual("medium", call["thinking_level"])
        self.assertEqual(16384, call["max_output_tokens"])
        self.assertIsNotNone(call["output_schema"])
        self.assertEqual("writer-model", call["model"])

    def test_writer_makes_exactly_one_call_for_a_valid_article(self) -> None:
        client = CaptureClient(writer_output())
        WriterNode(client, "writer-model", body_extractor())(
            {"article": ARTICLE, "revision_brief": []}
        )
        self.assertEqual(1, len(client.calls))

    def test_empty_cleaned_content_is_a_source_error(self) -> None:
        from daily_chip_news.sources import SourceError

        client = CaptureClient(writer_output())
        writer = WriterNode(client, "writer-model", body_extractor(""))
        with self.assertRaises(SourceError):
            writer({"article": ARTICLE, "revision_brief": []})
        self.assertEqual([], client.calls)

    def test_content_is_truncated_by_the_configured_budget(self) -> None:
        client = CaptureClient(writer_output())
        writer = WriterNode(
            client, "writer-model", body_extractor("x" * 5000), max_content_chars=2000
        )
        writer({"article": ARTICLE, "revision_brief": []})
        content = client.calls[0]["payload"]["raw_content"]
        self.assertLessEqual(len(content), 2000 + len("\n[content truncated]"))
        self.assertIn("[content truncated]", content)


class ReviewerContextTests(unittest.TestCase):
    def test_reviewer_uses_lower_reasoning_and_its_own_schema(self) -> None:
        client = CaptureClient(review("PASS"))
        ReviewerNode(
            client, "review-model", thinking_level="low", max_output_tokens=8192
        )({"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT})
        call = client.calls[0]
        self.assertEqual("reviewer", call["purpose"])
        self.assertEqual("low", call["thinking_level"])
        self.assertEqual(8192, call["max_output_tokens"])
        self.assertIsNotNone(call["output_schema"])
        self.assertEqual("review-model", call["model"])

    def test_reviewer_cannot_pass_below_qa_threshold(self) -> None:
        weak_pass = review("PASS")
        weak_pass["scores"]["factuality"] = 7
        client = CaptureClient(weak_pass)
        with self.assertRaises(SchemaError):
            ReviewerNode(client, "review-model")(
                {"article": ARTICLE, "research_notes": RESEARCH, "draft": DRAFT}
            )

    def test_writer_and_reviewer_keep_separate_model_routing(self) -> None:
        client = CaptureClient(writer_output(), review("PASS"))
        writer_update = WriterNode(client, "writer-model", body_extractor())(
            {"article": ARTICLE, "revision_brief": []}
        )
        ReviewerNode(client, "review-model")(
            {"article": ARTICLE, **writer_update}
        )
        self.assertEqual(
            ["writer-model", "review-model"],
            [call["model"] for call in client.calls],
        )


if __name__ == "__main__":
    unittest.main()
