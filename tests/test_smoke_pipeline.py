"""End-to-end pipeline smoke test with every HTTP dependency mocked."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import patch

import requests
from _support import (
    DRAFT,
    FakeResponse,
    gemini_response,
    make_settings,
    research_output,
    review,
)

from daily_chip_news.outcomes import RunOutcome
from daily_chip_news.runner import run_daily

FEED_URL = "https://feed.example/rss"
STORY_URL = "https://news.example/hbm-capacity"
BODY = "HBM 供应商将产能目标上调，原文给出了具体数字与时间点。\n\n" * 30

REVISED_DRAFT = {
    **DRAFT,
    "summary": "一家 HBM 供应商上调产能目标，原文未披露新增产线细节。",
}


class SmokeFeed:
    def __init__(self, entries, feed_title: str = "Smoke Feed") -> None:
        self.entries = list(entries)
        self.feed = {"title": feed_title}
        self.status = 200
        self.bozo = False
        self.bozo_exception = None


class RoutedHTTP:
    """Routes reader, Gemini and Telegram traffic to scripted responses."""

    def __init__(self, *gemini_payloads) -> None:
        self.gemini = list(gemini_payloads)
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"method": "GET", "url": url})
        if url.startswith("https://r.jina.ai/"):
            return FakeResponse(200, text=BODY)
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url: str, **kwargs):
        self.calls.append({"method": "POST", "url": url, "json": kwargs.get("json")})
        if "generativelanguage.googleapis.com" in url:
            if not self.gemini:
                raise AssertionError("No scripted Gemini response left")
            return self.gemini.pop(0)
        if "api.telegram.org" in url:
            return FakeResponse(200, {"ok": True})
        raise AssertionError(f"Unexpected POST {url}")

    def gemini_requests(self) -> list[dict]:
        return [
            call["json"] for call in self.calls if "generativelanguage" in call["url"]
        ]

    def telegram_messages(self) -> list[str]:
        return [
            call["json"]["text"]
            for call in self.calls
            if "api.telegram.org" in call["url"]
        ]


def smoke_feed() -> SmokeFeed:
    return SmokeFeed(
        [
            {
                "title": "HBM supplier raises capacity target",
                "link": STORY_URL,
                "published": datetime.now(UTC).isoformat(),
            }
        ]
    )


class PipelineSmokeTests(unittest.TestCase):
    def run_pipeline(self, http: RoutedHTTP, *, publish: bool = True) -> dict:
        settings = make_settings(
            publish_enabled=publish,
            source_feeds=(FEED_URL,),
            articles_per_feed=2,
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "daily_chip_news.sources.feedparser.parse", lambda url: smoke_feed()
                )
            )
            stack.enter_context(
                patch.object(
                    requests.Session,
                    "get",
                    lambda self, url, **kwargs: http.get(url, **kwargs),
                )
            )
            stack.enter_context(
                patch.object(
                    requests.Session,
                    "post",
                    lambda self, url, **kwargs: http.post(url, **kwargs),
                )
            )
            return run_daily(settings, alert_publisher=None)

    def test_standard_path_publishes_one_candidate(self) -> None:
        http = RoutedHTTP(
            gemini_response(research_output()),
            gemini_response(dict(DRAFT)),
            gemini_response(review("PASS")),
        )
        result = self.run_pipeline(http)
        self.assertEqual(RunOutcome.SUCCESS.value, result["run_outcome"])
        self.assertEqual(1, result["discovered"])
        self.assertEqual(1, result["selected"])
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, result["published"])
        self.assertEqual(0, result["skipped"])
        self.assertEqual(0, result["failed"])
        self.assertEqual(0, result["revisions"])
        self.assertEqual(3, len(http.gemini_requests()))
        messages = http.telegram_messages()
        self.assertEqual(1, len(messages))
        self.assertIn(DRAFT["telegram_copy"], messages[0])
        self.assertIn(STORY_URL, messages[0])

    def test_revision_loop_is_driven_by_reviewer_feedback(self) -> None:
        http = RoutedHTTP(
            gemini_response(research_output()),
            gemini_response(dict(DRAFT)),
            gemini_response(review("REVISE")),
            gemini_response(dict(REVISED_DRAFT)),
            gemini_response(review("PASS")),
        )
        result = self.run_pipeline(http)
        self.assertEqual(RunOutcome.SUCCESS.value, result["run_outcome"])
        self.assertEqual(1, result["published"])
        self.assertEqual(1, result["revisions"])
        requests_made = http.gemini_requests()
        self.assertEqual(5, len(requests_made))
        revise_instruction = requests_made[3]["contents"][0]["parts"][0]["text"]
        self.assertIn("previous_draft", revise_instruction)
        self.assertIn("删除缺乏依据的表述", revise_instruction)
        self.assertIn(REVISED_DRAFT["telegram_copy"], http.telegram_messages()[0])

    def test_reviewer_reject_skips_the_candidate(self) -> None:
        http = RoutedHTTP(
            gemini_response(research_output()),
            gemini_response(dict(DRAFT)),
            gemini_response(review("REJECT")),
        )
        result = self.run_pipeline(http)
        self.assertEqual(RunOutcome.SUCCESS.value, result["run_outcome"])
        self.assertEqual(0, result["published"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual(1, result["skipped_rejected"])
        self.assertEqual([], http.telegram_messages())

    def test_researcher_skip_stops_before_writing(self) -> None:
        http = RoutedHTTP(
            gemini_response(
                {"decision": "SKIP", "reason": "off scope", "topic": "", "notes": []}
            )
        )
        result = self.run_pipeline(http)
        self.assertEqual(1, result["skipped_not_relevant"])
        self.assertEqual(1, len(http.gemini_requests()))
        self.assertEqual([], http.telegram_messages())

    def test_retry_then_success_still_publishes(self) -> None:
        http = RoutedHTTP(
            FakeResponse(503, text=""),
            gemini_response(research_output()),
            gemini_response(dict(DRAFT)),
            gemini_response(review("PASS")),
        )
        with patch("daily_chip_news.gemini.time.sleep", lambda seconds: None):
            result = self.run_pipeline(http)
        self.assertEqual(1, result["published"])
        self.assertEqual(4, len(http.gemini_requests()))

    def test_dry_run_publishes_nothing_but_completes(self) -> None:
        http = RoutedHTTP(
            gemini_response(research_output()),
            gemini_response(dict(DRAFT)),
            gemini_response(review("PASS")),
        )
        result = self.run_pipeline(http, publish=False)
        self.assertEqual(1, result["published"])
        self.assertEqual([], http.telegram_messages())


if __name__ == "__main__":
    unittest.main()
