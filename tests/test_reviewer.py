from __future__ import annotations

import unittest

from _support import (  # noqa: F401
    CANDIDATE,
    DRAFT,
    RESEARCH,
    SRC,
    CaptureClient,
    review,
)

from daily_chip_news.config import NodeProfile
from daily_chip_news.nodes import ReviewerNode
from daily_chip_news.schemas import SchemaError


def node(client, **kwargs) -> ReviewerNode:
    profile = kwargs.pop("profile", NodeProfile(model="reviewer-model"))
    return ReviewerNode(client, profile, **kwargs)


def review_state() -> dict:
    return {
        "candidate": dict(CANDIDATE),
        "research_notes": dict(RESEARCH),
        "draft": dict(DRAFT),
        "revision_count": 0,
        "status": "DRAFTED",
    }


class ReviewerDecisionTests(unittest.TestCase):
    def test_pass_is_returned_as_is(self) -> None:
        client = CaptureClient(review("PASS"))
        update = node(client)(review_state())
        self.assertEqual("PASS", update["review"]["status"])
        self.assertEqual([], update["review"]["revision_brief"])

    def test_revise_carries_instructions(self) -> None:
        client = CaptureClient(review("REVISE"))
        update = node(client)(review_state())
        self.assertEqual("REVISE", update["review"]["status"])
        self.assertTrue(update["review"]["revision_brief"])

    def test_reject_carries_issues(self) -> None:
        client = CaptureClient(review("REJECT"))
        update = node(client)(review_state())
        self.assertEqual("REJECT", update["review"]["status"])
        self.assertTrue(update["review"]["issues"])

    def test_reviewer_receives_notes_draft_and_rubric_only(self) -> None:
        client = CaptureClient(review("PASS"))
        node(client)(review_state())
        payload = client.calls[0]["payload"]
        self.assertEqual(RESEARCH["notes"], payload["research_notes"]["notes"])
        self.assertEqual(RESEARCH["entities"], payload["research_notes"]["entities"])
        self.assertEqual(
            RESEARCH["key_numbers"], payload["research_notes"]["key_numbers"]
        )
        self.assertEqual(DRAFT, payload["draft"])
        self.assertTrue(payload["rubric"])
        self.assertNotIn("raw_content", repr(payload))

    def test_profile_drives_model_thinking_and_output_tokens(self) -> None:
        client = CaptureClient(review("PASS"))
        profile = NodeProfile(
            model="reviewer-model", thinking_level="low", max_output_tokens=1024
        )
        node(client, profile=profile)(review_state())
        call = client.calls[0]
        self.assertEqual("reviewer-model", call["model"])
        self.assertEqual("low", call["thinking_level"])
        self.assertEqual(1024, call["max_output_tokens"])
        self.assertEqual("reviewer", call["purpose"])


class ReviewerValidationTests(unittest.TestCase):
    def test_unknown_status_is_rejected(self) -> None:
        client = CaptureClient(review("MAYBE"))
        with self.assertRaises(SchemaError):
            node(client)(review_state())

    def test_pass_with_major_issue_is_rejected(self) -> None:
        client = CaptureClient(
            review("PASS", issues=[{"severity": "major", "problem": "unsupported"}])
        )
        with self.assertRaises(SchemaError):
            node(client)(review_state())

    def test_pass_with_low_factuality_is_rejected(self) -> None:
        client = CaptureClient(
            review("PASS", scores={"factuality": 5, "relevance": 9, "clarity": 9})
        )
        with self.assertRaises(SchemaError):
            node(client)(review_state())

    def test_revise_without_instructions_is_rejected(self) -> None:
        client = CaptureClient(review("REVISE", revision_brief=[]))
        with self.assertRaises(SchemaError):
            node(client)(review_state())

    def test_reject_without_issues_is_rejected(self) -> None:
        client = CaptureClient(review("REJECT", issues=[]))
        with self.assertRaises(SchemaError):
            node(client)(review_state())

    def test_pass_with_revision_brief_is_rejected(self) -> None:
        client = CaptureClient(review("PASS", revision_brief=["fix it"]))
        with self.assertRaises(SchemaError):
            node(client)(review_state())


if __name__ == "__main__":
    unittest.main()
