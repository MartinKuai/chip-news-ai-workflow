from __future__ import annotations

import unittest

import requests
from _support import CANDIDATE, DRAFT, SRC, FakeResponse, FakeSession  # noqa: F401

from daily_chip_news.publisher import PublisherError, PublisherNode, TelegramPublisher


def publisher(session: FakeSession, **kwargs) -> TelegramPublisher:
    return TelegramPublisher(
        "bot-token",
        "-100123",
        session=session,
        logger=None,
        **kwargs,
    )


def published_state() -> dict:
    return {
        "candidate": dict(CANDIDATE),
        "draft": dict(DRAFT),
        "review": {"status": "PASS"},
        "status": "PASS",
    }


class TelegramDeliveryTests(unittest.TestCase):
    def test_markdown_delivery_includes_the_source_url(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        publisher(session).publish(DRAFT["telegram_copy"], CANDIDATE["url"])
        method, url, kwargs = session.calls[0]
        self.assertEqual("POST", method)
        self.assertIn("api.telegram.org/botbot-token/sendMessage", url)
        self.assertEqual("Markdown", kwargs["json"]["parse_mode"])
        self.assertIn(DRAFT["telegram_copy"], kwargs["json"]["text"])
        self.assertIn(CANDIDATE["url"], kwargs["json"]["text"])

    def test_markdown_failure_falls_back_to_plain_text(self) -> None:
        session = FakeSession(
            FakeResponse(400, {"ok": False}), FakeResponse(200, {"ok": True})
        )
        publisher(session).publish("copy", "https://example.com/a")
        self.assertEqual(2, len(session.calls))
        self.assertNotIn("parse_mode", session.calls[1][2]["json"])

    def test_permanent_failure_is_a_global_publisher_error(self) -> None:
        session = FakeSession(FakeResponse(401, {"ok": False}))
        with self.assertRaises(PublisherError) as context:
            publisher(session).publish("copy", "https://example.com/a")
        self.assertTrue(context.exception.global_failure)
        self.assertFalse(context.exception.transient)

    def test_rate_limit_is_transient(self) -> None:
        session = FakeSession(FakeResponse(429, {"ok": False}))
        with self.assertRaises(PublisherError) as context:
            publisher(session).publish("copy", "https://example.com/a")
        self.assertTrue(context.exception.transient)
        self.assertFalse(context.exception.global_failure)

    def test_network_failure_is_transient_and_does_not_leak_the_token(self) -> None:
        session = FakeSession(requests.ConnectionError("boom"))
        with self.assertRaises(PublisherError) as context:
            publisher(session).publish("copy", "https://example.com/a")
        self.assertTrue(context.exception.transient)
        self.assertNotIn("bot-token", str(context.exception))

    def test_disabled_publishing_skips_the_network(self) -> None:
        session = FakeSession()
        publisher(session, enabled=False).publish("copy", "https://example.com/a")
        self.assertEqual([], session.calls)

    def test_alert_has_no_source_url(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        publisher(session).publish_alert("⚠️ Daily Chip News 运行失败")
        self.assertNotIn("原文", session.calls[0][2]["json"]["text"])


class PublisherNodeTests(unittest.TestCase):
    def test_pass_state_is_published(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        update = PublisherNode(publisher(session))(published_state())
        self.assertTrue(update["published"])

    def test_item_without_reviewer_pass_is_refused(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        state = {**published_state(), "review": {"status": "REVISE"}}
        with self.assertRaises(PublisherError):
            PublisherNode(publisher(session))(state)
        self.assertEqual([], session.calls)

    def test_graph_status_must_be_pass(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        state = {**published_state(), "status": "REVISE"}
        with self.assertRaises(PublisherError):
            PublisherNode(publisher(session))(state)


if __name__ == "__main__":
    unittest.main()
