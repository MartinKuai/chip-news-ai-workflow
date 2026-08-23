from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.sources import SourceCollectionError, collect_articles


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


if __name__ == "__main__":
    unittest.main()
