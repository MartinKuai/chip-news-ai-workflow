"""RSS candidate collection and deterministic article extraction."""

from __future__ import annotations

import time
from collections.abc import Iterable

import feedparser
import requests

from .config import RSS_FEEDS
from .schemas import Article


class SourceError(RuntimeError):
    """Raised when source infrastructure fails; never converted to SKIP."""


def collect_articles(
    articles_per_feed: int,
    *,
    feeds: Iterable[str] = RSS_FEEDS,
) -> list[Article]:
    """Collect a small, URL-deduplicated candidate set from RSS feeds."""
    articles: list[Article] = []
    seen_urls: set[str] = set()
    for feed_url in feeds:
        feed = feedparser.parse(feed_url)
        if getattr(feed, "bozo", False) and not getattr(feed, "entries", []):
            raise SourceError(f"RSS source could not be parsed: {feed_url}")
        source = str(feed.feed.get("title", feed_url))
        for entry in feed.entries[:articles_per_feed]:
            url = str(entry.get("link", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            articles.append(
                {
                    "title": str(entry.get("title", "Untitled article")),
                    "url": url,
                    "source": source,
                    "published_at": str(
                        entry.get("published", entry.get("updated", ""))
                    ),
                }
            )
    return articles


class ArticleExtractor:
    """Fetch readable article text through Jina Reader with bounded retries."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_attempts: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._session = session or requests.Session()

    def __call__(self, url: str) -> str:
        reader_url = f"https://r.jina.ai/{url}"
        last_status: int | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.get(reader_url, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt == self._max_attempts:
                    raise SourceError("Article extraction network request failed") from exc
                time.sleep(float(attempt))
                continue
            last_status = response.status_code
            if response.status_code == 200 and len(response.text.strip()) >= 200:
                return response.text
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_attempts:
                time.sleep(float(attempt))
                continue
            break
        if last_status == 200:
            raise SourceError("Article extraction returned insufficient content")
        raise SourceError(f"Article extraction failed with HTTP {last_status}")
