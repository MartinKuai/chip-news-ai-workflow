from __future__ import annotations

import unittest

from _support import CANDIDATE, DRAFT, RESEARCH, SRC, CaptureClient  # noqa: F401

from daily_chip_news.config import NodeProfile
from daily_chip_news.nodes import WriterNode
from daily_chip_news.schemas import SchemaError


def node(client, **kwargs) -> WriterNode:
    profile = kwargs.pop("profile", NodeProfile(model="writer-model"))
    return WriterNode(client, profile, **kwargs)


def compose_state() -> dict:
    return {
        "candidate": dict(CANDIDATE),
        "research_notes": dict(RESEARCH),
        "revision_brief": [],
        "revision_count": 0,
        "status": "RESEARCHED",
    }


class WriterComposeTests(unittest.TestCase):
    def test_compose_payload_uses_notes_brief_and_schema(self) -> None:
        client = CaptureClient(dict(DRAFT))
        update = node(client)(compose_state())
        payload = client.calls[0]["payload"]
        self.assertEqual("DRAFTED", update["status"])
        self.assertEqual(DRAFT, update["draft"])
        self.assertEqual("writer", client.calls[0]["purpose"])
        self.assertEqual(RESEARCH["notes"], payload["research_notes"]["notes"])
        self.assertEqual([], payload["revision_brief"])
        self.assertNotIn("previous_draft", payload)
        self.assertIn("output_schema", payload)

    def test_compose_never_sends_the_article_body(self) -> None:
        client = CaptureClient(dict(DRAFT))
        node(client)(compose_state())
        serialized = repr(client.calls[0]["payload"])
        self.assertNotIn("raw_content", serialized)
        self.assertNotIn("body", serialized.lower())

    def test_profile_drives_model_thinking_and_output_tokens(self) -> None:
        client = CaptureClient(dict(DRAFT))
        profile = NodeProfile(
            model="writer-model", thinking_level="low", max_output_tokens=2560
        )
        node(client, profile=profile)(compose_state())
        call = client.calls[0]
        self.assertEqual("writer-model", call["model"])
        self.assertEqual("low", call["thinking_level"])
        self.assertEqual(2560, call["max_output_tokens"])


class WriterReviseTests(unittest.TestCase):
    def test_revise_receives_previous_draft_and_instructions(self) -> None:
        client = CaptureClient(dict(DRAFT))
        state = {
            **compose_state(),
            "draft": dict(DRAFT),
            "revision_brief": ["删除缺乏依据的表述"],
            "revision_count": 1,
            "status": "REVISE",
        }
        node(client)(state)
        payload = client.calls[0]["payload"]
        self.assertEqual(DRAFT, payload["previous_draft"])
        self.assertEqual(["删除缺乏依据的表述"], payload["revision_brief"])
        self.assertEqual(RESEARCH["notes"], payload["research_notes"]["notes"])

    def test_revise_uses_the_same_fact_source_as_compose(self) -> None:
        client = CaptureClient(dict(DRAFT), dict(DRAFT))
        writer = node(client)
        writer(compose_state())
        writer(
            {
                **compose_state(),
                "draft": dict(DRAFT),
                "revision_brief": ["tighten summary"],
                "revision_count": 1,
                "status": "REVISE",
            }
        )
        compose_notes = client.calls[0]["payload"]["research_notes"]
        revise_notes = client.calls[1]["payload"]["research_notes"]
        self.assertEqual(compose_notes, revise_notes)


class WriterValidationTests(unittest.TestCase):
    def test_empty_key_facts_is_rejected(self) -> None:
        client = CaptureClient({**DRAFT, "key_facts": []})
        with self.assertRaises(SchemaError):
            node(client)(compose_state())

    def test_missing_field_is_rejected(self) -> None:
        value = dict(DRAFT)
        value.pop("telegram_copy")
        client = CaptureClient(value)
        with self.assertRaises(SchemaError):
            node(client)(compose_state())


if __name__ == "__main__":
    unittest.main()
