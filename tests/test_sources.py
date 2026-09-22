from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.sources import (
    SourceCollectionError,
    clean_extracted_text,
    collect_articles,
    select_candidates,
)


FEEDS = tuple(f"https://feed-{index}.example/rss" for index in range(5))


def entry(index: int) -> dict[str, str]:
    return {
        "title": f"Article {index}",
        "link": f"https://articles.example/{index}",
        "published": "2026-08-24",
    }


class FakeFeed:
    def __init__(
        self,
        index: int,
        *,
        bozo: bool = False,
        entries: list[dict[str, str]] | None = None,
        error: Exception | None = None,
        status: int = 200,
    ) -> None:
        self.feed = {"title": f"Source {index}"}
        self.entries = [entry(index)] if entries is None else entries
        self.bozo = bozo
        self.bozo_exception = error
        self.status = status


class SourceCollectionTests(unittest.TestCase):
    def test_all_five_sources_succeed(self) -> None:
        feeds = {url: FakeFeed(index) for index, url in enumerate(FEEDS)}

        result = collect_articles(1, feeds=FEEDS, parser=feeds.__getitem__)

        self.assertEqual(5, result.sources_total)
        self.assertEqual(5, result.sources_ok)
        self.assertEqual(0, result.sources_failed)
        self.assertEqual(5, len(result.articles))

    def test_one_failed_source_keeps_other_candidates(self) -> None:
        feeds = {url: FakeFeed(index) for index, url in enumerate(FEEDS)}
        feeds[FEEDS[2]] = FakeFeed(
            2,
            bozo=True,
            entries=[],
            error=ValueError("invalid feed"),
        )

        result = collect_articles(1, feeds=FEEDS, parser=feeds.__getitem__)

        self.assertEqual(4, result.sources_ok)
        self.assertEqual(1, result.sources_failed)
        self.assertEqual(4, len(result.articles))
        self.assertEqual("ValueError", result.failures[0].error)

    def test_first_source_exception_does_not_stop_later_sources(self) -> None:
        calls: list[str] = []

        def parser(url: str):
            calls.append(url)
            if url == FEEDS[0]:
                raise TimeoutError("source timeout")
            return FakeFeed(FEEDS.index(url))

        result = collect_articles(1, feeds=FEEDS, parser=parser)

        self.assertEqual(list(FEEDS), calls)
        self.assertEqual(4, result.sources_ok)
        self.assertEqual(1, result.sources_failed)
        self.assertEqual(4, len(result.articles))

    def test_http_error_without_entries_is_source_failure(self) -> None:
        feeds = {url: FakeFeed(index) for index, url in enumerate(FEEDS)}
        feeds[FEEDS[1]] = FakeFeed(1, entries=[], status=503)

        result = collect_articles(1, feeds=FEEDS, parser=feeds.__getitem__)

        self.assertEqual(4, result.sources_ok)
        self.assertEqual(1, result.sources_failed)
        self.assertEqual("HTTPError", result.failures[0].error)

    def test_all_sources_failed_raises_collection_error(self) -> None:
        def parser(url: str):
            return FakeFeed(
                FEEDS.index(url),
                bozo=True,
                entries=[],
                error=ValueError("invalid feed"),
            )

        with self.assertRaises(SourceCollectionError) as context:
            collect_articles(1, feeds=FEEDS, parser=parser)

        self.assertEqual(5, context.exception.result.sources_total)
        self.assertEqual(0, context.exception.result.sources_ok)
        self.assertEqual(5, context.exception.result.sources_failed)
        self.assertEqual([], context.exception.result.articles)

    def test_bozo_feed_with_entries_is_usable(self) -> None:
        feeds = {
            FEEDS[0]: FakeFeed(
                0,
                bozo=True,
                error=ValueError("recoverable warning"),
            )
        }

        result = collect_articles(1, feeds=(FEEDS[0],), parser=feeds.__getitem__)

        self.assertEqual(1, result.sources_ok)
        self.assertEqual(0, result.sources_failed)
        self.assertEqual(1, len(result.articles))


    def test_trendforce_parser_error_stays_source_scoped(self) -> None:
        def parser(url: str):
            if url == FEEDS[3]:
                raise TypeError("SAXParseException-like parser failure")
            return FakeFeed(FEEDS.index(url))

        result = collect_articles(1, feeds=FEEDS, parser=parser)

        self.assertEqual(5, result.sources_total)
        self.assertEqual(4, result.sources_ok)
        self.assertEqual(1, result.sources_failed)
        self.assertEqual("TypeError", result.failures[0].error)
        self.assertEqual(4, len(result.articles))

    def test_malformed_xml_feed_does_not_stop_the_others(self) -> None:
        feeds = {url: FakeFeed(index) for index, url in enumerate(FEEDS)}
        feeds[FEEDS[3]] = FakeFeed(
            3,
            bozo=True,
            entries=[],
            error=ValueError("syntax error: line 1, column 0"),
        )

        result = collect_articles(1, feeds=FEEDS, parser=feeds.__getitem__)

        self.assertEqual(4, result.sources_ok)
        self.assertEqual(1, result.sources_failed)
        self.assertEqual(4, len(result.articles))


class CleanExtractedTextTests(unittest.TestCase):
    def test_reader_metadata_block_is_removed(self) -> None:
        raw = (
            "Title: HBM update\n\n"
            "URL Source: https://example.com/hbm\n\n"
            "Published Time: 2026-08-24\n\n"
            "Markdown Content:\n"
            "First paragraph.\n\n"
            "Second paragraph.\n"
        )
        cleaned = clean_extracted_text(raw, max_chars=1000)
        self.assertEqual("First paragraph.\n\nSecond paragraph.", cleaned)

    def test_images_rules_and_duplicate_lines_are_dropped(self) -> None:
        raw = (
            "Markdown Content:\n"
            "![cover](https://example.com/cover.png)\n\n"
            "---\n\n"
            "Repeated line.\n"
            "Repeated line.\n\n\n\n"
            "Tail line.\n"
        )
        cleaned = clean_extracted_text(raw, max_chars=1000)
        self.assertEqual("Repeated line.\n\nTail line.", cleaned)

    def test_long_text_is_truncated_at_a_paragraph_boundary(self) -> None:
        paragraphs = [f"Paragraph {index} " + "x" * 80 for index in range(40)]
        cleaned = clean_extracted_text("\n\n".join(paragraphs), max_chars=600)
        self.assertLessEqual(len(cleaned), 600 + len("\n[content truncated]"))
        self.assertIn("[content truncated]", cleaned)
        self.assertNotIn("Paragraph 39", cleaned)

    def test_empty_input_stays_empty(self) -> None:
        self.assertEqual("", clean_extracted_text("", max_chars=1000))
        self.assertEqual("", clean_extracted_text("\n\n\n", max_chars=1000))


class CandidateSelectionTests(unittest.TestCase):
    def article(
        self,
        index: int,
        *,
        source: str = "Source A",
        title: str | None = None,
        url: str | None = None,
        published: str = "Mon, 21 Sep 2026 10:00:00 +0000",
    ) -> dict[str, str]:
        return {
            "title": title or f"Semiconductor story number {index}",
            "source": source,
            "url": url or f"https://example.com/story-{index}",
            "published_at": published,
        }

    def test_limit_caps_the_selection(self) -> None:
        items = [self.article(index) for index in range(10)]
        selected = select_candidates(items, limit=6)
        self.assertEqual(6, len(selected))

    def test_canonical_dedupe_ignores_tracking_params_and_fragments(self) -> None:
        items = [
            self.article(
                1, url="https://www.example.com/story?utm_source=rss#section"
            ),
            self.article(2, url="https://example.com/story"),
            self.article(3, url="https://example.com/story?fbclid=abc"),
        ]
        selected = select_candidates(items, limit=6)
        self.assertEqual(1, len(selected))

    def test_obvious_junk_is_removed(self) -> None:
        items = [
            self.article(1, title="Advertisement: buy this now"),
            self.article(2, title="We're hiring engineers now"),
            self.article(3, title="Hi"),
            self.article(4, title="Home"),
            self.article(5, url="https://example.com/jobs/123"),
            self.article(6),
        ]
        selected = select_candidates(items, limit=6)
        self.assertEqual(1, len(selected))
        self.assertEqual("https://example.com/story-6", selected[0]["url"])

    def test_short_but_real_titles_survive(self) -> None:
        items = [
            self.article(1, title="HBM 量产"),
            self.article(2, title="Intel cuts jobs"),
        ]
        selected = select_candidates(items, limit=6)
        self.assertEqual(2, len(selected))

    def test_round_robin_preserves_source_diversity(self) -> None:
        items = [
            self.article(1, source="Source A"),
            self.article(2, source="Source A"),
            self.article(3, source="Source A"),
            self.article(4, source="Source B"),
            self.article(5, source="Source B"),
            self.article(6, source="Source B"),
        ]
        selected = select_candidates(items, limit=4)
        self.assertEqual(
            ["Source A", "Source B", "Source A", "Source B"],
            [item["source"] for item in selected],
        )

    def test_recency_orders_within_a_source(self) -> None:
        items = [
            self.article(1, published="Mon, 21 Sep 2026 08:00:00 +0000"),
            self.article(2, published="Mon, 21 Sep 2026 12:00:00 +0000"),
            self.article(3, published="Mon, 21 Sep 2026 10:00:00 +0000"),
        ]
        selected = select_candidates(items, limit=1)
        self.assertEqual("https://example.com/story-2", selected[0]["url"])

    def test_zero_limit_selects_nothing(self) -> None:
        items = [self.article(1)]
        self.assertEqual([], select_candidates(items, limit=0))

    def test_selection_is_deterministic(self) -> None:
        items = [self.article(index) for index in range(8)]
        first = select_candidates(items, limit=6)
        second = select_candidates(items, limit=6)
        self.assertEqual(
            [item["url"] for item in first], [item["url"] for item in second]
        )


if __name__ == "__main__":
    unittest.main()
