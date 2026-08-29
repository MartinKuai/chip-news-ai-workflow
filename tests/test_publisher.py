from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.publisher import TelegramPublisher


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeSession:
    def __init__(self, *responses: FakeResponse):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class PublisherTests(unittest.TestCase):
    def test_publish_alert_uses_deterministic_telegram_path_without_source_url(self):
        session = FakeSession(FakeResponse(200))
        TelegramPublisher(
            "test-token",
            "test-chat",
            session=session,
        ).publish_alert("⚠️ Daily Chip News 运行失败")

        url, kwargs = session.calls[0]
        self.assertEqual(
            "https://api.telegram.org/bottest-token/sendMessage",
            url,
        )
        self.assertEqual("test-chat", kwargs["json"]["chat_id"])
        self.assertEqual("⚠️ Daily Chip News 运行失败", kwargs["json"]["text"])
        self.assertNotIn("原文：", kwargs["json"]["text"])

    def test_publish_alert_retries_plain_text_after_markdown_400(self):
        session = FakeSession(FakeResponse(400), FakeResponse(200))
        TelegramPublisher(
            "test-token",
            "test-chat",
            session=session,
        ).publish_alert("alert")

        self.assertEqual(2, len(session.calls))
        self.assertNotIn("parse_mode", session.calls[1][1]["json"])


if __name__ == "__main__":
    unittest.main()
