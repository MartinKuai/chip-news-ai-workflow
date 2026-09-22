from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import requests
from _support import SRC, FakeResponse, FakeSession  # noqa: F401

from daily_chip_news.sources import (
    ArticleExtractor,
    SourceCollectionError,
    SourceError,
    candidate_id,
    canonical_url,
    clean_extracted_text,
    collect_articles,
    published_timestamp,
    select_candidates,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC).timestamp()


def iso_hours_ago(hours: float) -> str:
    moment = datetime.fromtimestamp(NOW - hours * 3600.0, tz=UTC)
    return moment.isoformat()


def candidate(
    url: str,
    *,
    title: str = "A useful semiconductor headline",
    source: str = "Feed A",
    published: str = "",
) -> dict:
    return {
        "id": candidate_id(url),
        "title": title,
        "url": url,
        "source": source,
        "published_at": published,
        "metadata": {},
    }


class FakeFeed:
    def __init__(
        self,
        entries=(),
        *,
        feed_title: str = "Feed",
        status: int = 200,
        bozo: bool = False,
        bozo_exception: Exception | None = None,
    ) -> None:
        self.entries = list(entries)
        self.feed = {"title": feed_title}
        self.status = status
        self.bozo = bozo
        self.bozo_exception = bozo_exception


def parser_for(mapping: dict[str, object]):
    def parse(url: str):
        result = mapping[url]
        if isinstance(result, Exception):
            raise result
        return result

    return parse


class CanonicalUrlTests(unittest.TestCase):
    def test_tracking_parameters_and_fragment_are_removed(self) -> None:
        value = canonical_url(
            "https://www.example.com/news/item/?utm_source=x&fbclid=y&id=7#top"
        )
        self.assertEqual("https://example.com/news/item?id=7", value)

    def test_host_case_and_trailing_slash_are_normalized(self) -> None:
        self.assertEqual(
            canonical_url("HTTP://WWW.Example.COM/a/"),
            canonical_url("http://example.com/a"),
        )

    def test_remaining_query_parameters_are_sorted(self) -> None:
        self.assertEqual(
            "https://example.com/a?a=1&b=2",
            canonical_url("https://example.com/a?b=2&a=1"),
        )


class CandidateIdTests(unittest.TestCase):
    def test_id_is_deterministic_across_tracking_variants(self) -> None:
        first = candidate_id("https://www.example.com/a?utm_source=x")
        second = candidate_id("https://example.com/a")
        self.assertEqual(first, second)
        self.assertEqual(10, len(first))

    def test_different_urls_get_different_ids(self) -> None:
        self.assertNotEqual(
            candidate_id("https://example.com/a"),
            candidate_id("https://example.com/b"),
        )


class SelectCandidateTests(unittest.TestCase):
    def test_canonical_duplicates_are_removed(self) -> None:
        selected = select_candidates(
            [
                candidate("https://www.example.com/a?utm_source=rss"),
                candidate("https://example.com/a"),
                candidate("https://example.com/a/"),
            ],
            limit=6,
        )
        self.assertEqual(1, len(selected))

    def test_garbage_is_filtered(self) -> None:
        junk = [
            candidate("https://example.com/1", title="hi"),
            candidate("https://example.com/2", title="Home"),
            candidate("https://example.com/3", title="We are hiring engineers"),
            candidate("https://example.com/jobs/4", title="Staff engineer"),
            candidate("https://example.com/5", title="Weekly newsletter"),
            candidate("", title="No URL at all"),
        ]
        self.assertEqual([], select_candidates(junk, limit=6))

    def test_recency_filter_drops_known_stale_items_only(self) -> None:
        selected = select_candidates(
            [
                candidate("https://example.com/fresh", published=iso_hours_ago(2)),
                candidate("https://example.com/stale", published=iso_hours_ago(200)),
                candidate("https://example.com/undated"),
            ],
            limit=6,
            max_age_hours=72,
            now=NOW,
        )
        urls = [item["url"] for item in selected]
        self.assertIn("https://example.com/fresh", urls)
        self.assertIn("https://example.com/undated", urls)
        self.assertNotIn("https://example.com/stale", urls)

    def test_recency_filter_can_be_disabled(self) -> None:
        selected = select_candidates(
            [candidate("https://example.com/stale", published=iso_hours_ago(5000))],
            limit=6,
            max_age_hours=0,
            now=NOW,
        )
        self.assertEqual(1, len(selected))

    def test_newest_item_leads_each_source(self) -> None:
        selected = select_candidates(
            [
                candidate("https://example.com/old", published=iso_hours_ago(30)),
                candidate("https://example.com/new", published=iso_hours_ago(1)),
            ],
            limit=6,
            now=NOW,
        )
        self.assertEqual("https://example.com/new", selected[0]["url"])

    def test_round_robin_keeps_sources_balanced(self) -> None:
        items = [
            candidate(f"https://a.example.com/{index}", source="A")
            for index in range(3)
        ] + [candidate("https://b.example.com/1", source="B")]
        selected = select_candidates(items, limit=4)
        self.assertEqual(["A", "B", "A", "A"], [item["source"] for item in selected])

    def test_candidate_limit_is_enforced(self) -> None:
        items = [candidate(f"https://example.com/{index}") for index in range(10)]
        self.assertEqual(6, len(select_candidates(items, limit=6)))
        self.assertEqual([], select_candidates(items, limit=0))

    def test_max_per_source_caps_one_source(self) -> None:
        items = [
            candidate(f"https://a.example.com/{index}", source="A")
            for index in range(5)
        ]
        selected = select_candidates(items, limit=6, max_per_source=2)
        self.assertEqual(2, len(selected))


class CollectArticlesTests(unittest.TestCase):
    def test_candidates_carry_source_metadata_and_ids(self) -> None:
        feed = FakeFeed(
            [
                {
                    "title": "Chip news",
                    "link": "https://example.com/a",
                    "published": "x",
                },
            ],
            feed_title="Example Feed",
        )
        result = collect_articles(
            2,
            feeds=["https://example.com/feed"],
            parser=parser_for({"https://example.com/feed": feed}),
        )
        self.assertEqual(1, result.sources_ok)
        entry = result.candidates[0]
        self.assertEqual("Example Feed", entry["source"])
        self.assertEqual("https://example.com/feed", entry["metadata"]["feed_url"])
        self.assertEqual(candidate_id("https://example.com/a"), entry["id"])

    def test_entries_per_feed_limit_is_applied(self) -> None:
        feed = FakeFeed(
            [
                {"title": f"Story {index}", "link": f"https://example.com/{index}"}
                for index in range(5)
            ]
        )
        result = collect_articles(
            2,
            feeds=["https://example.com/feed"],
            parser=parser_for({"https://example.com/feed": feed}),
        )
        self.assertEqual(2, len(result.candidates))

    def test_duplicate_urls_across_feeds_are_collected_once(self) -> None:
        entries = [{"title": "Same story", "link": "https://example.com/same"}]
        result = collect_articles(
            2,
            feeds=["https://a.example/feed", "https://b.example/feed"],
            parser=parser_for(
                {
                    "https://a.example/feed": FakeFeed(entries, feed_title="A"),
                    "https://b.example/feed": FakeFeed(entries, feed_title="B"),
                }
            ),
        )
        self.assertEqual(1, len(result.candidates))

    def test_broken_feed_is_isolated(self) -> None:
        good = FakeFeed([{"title": "Good", "link": "https://example.com/good"}])
        broken = FakeFeed(bozo=True, bozo_exception=ValueError("bad xml"))
        result = collect_articles(
            2,
            feeds=["https://good.example/feed", "https://bad.example/feed"],
            parser=parser_for(
                {
                    "https://good.example/feed": good,
                    "https://bad.example/feed": broken,
                }
            ),
        )
        self.assertEqual(1, result.sources_ok)
        self.assertEqual(1, result.sources_failed)
        self.assertEqual("ValueError", result.failures[0].error)

    def test_parser_exception_is_recorded_as_source_failure(self) -> None:
        result = collect_articles(
            2,
            feeds=["https://a.example/feed", "https://b.example/feed"],
            parser=parser_for(
                {
                    "https://a.example/feed": FakeFeed([]),
                    "https://b.example/feed": requests.RequestException("boom"),
                }
            ),
        )
        self.assertEqual(1, result.sources_failed)
        self.assertEqual("RequestException", result.failures[0].error)

    def test_all_sources_unavailable_raises_with_statistics(self) -> None:
        with self.assertRaises(SourceCollectionError) as context:
            collect_articles(
                2,
                feeds=["https://bad.example/feed"],
                parser=parser_for({"https://bad.example/feed": FakeFeed(status=503)}),
            )
        self.assertEqual(1, context.exception.result.sources_total)
        self.assertEqual(0, context.exception.result.sources_ok)
        self.assertEqual(1, context.exception.result.sources_failed)


class CleanExtractedTextTests(unittest.TestCase):
    def test_reader_metadata_header_is_dropped(self) -> None:
        raw = (
            "Title: Some article\n"
            "URL Source: https://example.com/a\n"
            "Published Time: 2026-09-23\n"
            "Markdown Content:\n"
            "Body line one\n"
        )
        self.assertEqual("Body line one", clean_extracted_text(raw, max_chars=1000))

    def test_noise_lines_are_dropped(self) -> None:
        raw = "Body line\nBody line\n\n![image](https://example.com/a.png)\n---\n"
        self.assertEqual("Body line", clean_extracted_text(raw, max_chars=1000))

    def test_paragraph_breaks_are_kept(self) -> None:
        raw = "First paragraph\n\nSecond paragraph\n"
        self.assertEqual(
            "First paragraph\n\nSecond paragraph",
            clean_extracted_text(raw, max_chars=1000),
        )

    def test_long_text_is_truncated_on_a_paragraph_boundary(self) -> None:
        raw = "first paragraph " * 20 + "\n\n" + "second paragraph " * 20
        cleaned = clean_extracted_text(raw, max_chars=200)
        self.assertTrue(cleaned.endswith("[content truncated]"))
        self.assertLessEqual(len(cleaned), 200 + len("\n[content truncated]"))


class ArticleExtractorTests(unittest.TestCase):
    def test_successful_extraction_returns_reader_text(self) -> None:
        session = FakeSession(FakeResponse(200, text="A" * 400))
        extractor = ArticleExtractor(session=session)
        self.assertEqual("A" * 400, extractor("https://example.com/a"))
        self.assertIn("https://r.jina.ai/https://example.com/a", session.calls[0][1])

    def test_transient_http_failure_is_retried(self) -> None:
        session = FakeSession(
            FakeResponse(503, text=""), FakeResponse(200, text="B" * 300)
        )
        with patch("daily_chip_news.sources.time.sleep", lambda seconds: None):
            text = ArticleExtractor(max_attempts=2, session=session)(
                "https://example.com/a"
            )
        self.assertEqual("B" * 300, text)

    def test_permanent_http_failure_raises_source_error(self) -> None:
        session = FakeSession(FakeResponse(404, text=""))
        with self.assertRaises(SourceError):
            ArticleExtractor(session=session)("https://example.com/a")

    def test_short_response_is_treated_as_failure(self) -> None:
        session = FakeSession(FakeResponse(200, text="too short"))
        with self.assertRaises(SourceError):
            ArticleExtractor(session=session)("https://example.com/a")

    def test_network_failure_is_retried_then_raised(self) -> None:
        session = FakeSession(
            requests.ConnectionError("boom"), requests.ConnectionError("boom")
        )
        with (
            patch("daily_chip_news.sources.time.sleep", lambda seconds: None),
            self.assertRaises(SourceError),
        ):
            ArticleExtractor(max_attempts=2, session=session)("https://example.com/a")


class PublishedTimestampTests(unittest.TestCase):
    def test_rfc822_and_iso_values_are_parsed(self) -> None:
        rfc = published_timestamp("Tue, 23 Sep 2026 02:00:00 GMT")
        iso = published_timestamp("2026-09-23T02:00:00Z")
        self.assertAlmostEqual(rfc, iso, delta=1.0)

    def test_unknown_values_return_zero(self) -> None:
        self.assertEqual(0.0, published_timestamp(""))
        self.assertEqual(0.0, published_timestamp("not a date"))


if __name__ == "__main__":
    unittest.main()
