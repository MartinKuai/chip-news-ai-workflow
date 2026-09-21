"""RSS candidate collection and deterministic article extraction."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

import feedparser
import requests

from .config import RSS_FEEDS
from .schemas import Article


class SourceError(RuntimeError):
    """Raised when source infrastructure fails; never converted to SKIP."""


@dataclass(frozen=True)
class SourceFailure:
    """A bounded, log-safe record for one unavailable RSS source."""

    source: str
    error: str


@dataclass(frozen=True)
class SourceCollectionResult:
    """Articles and availability statistics from one RSS collection pass."""

    articles: list[Article]
    sources_total: int
    sources_ok: int
    sources_failed: int
    failures: list[SourceFailure]


class SourceCollectionError(SourceError):
    """Raised when every configured RSS source is unavailable."""

    def __init__(self, result: SourceCollectionResult) -> None:
        self.result = result
        super().__init__("All RSS sources are unavailable")


def _safe_source_name(value: object) -> str:
    """Keep source labels single-line and bounded for run summaries."""
    return " ".join(str(value).split())[:200]


def collect_articles(
    articles_per_feed: int,
    *,
    feeds: Iterable[str] = RSS_FEEDS,
    parser: Callable[[str], Any] = feedparser.parse,
) -> SourceCollectionResult:
    """Collect candidates while isolating failures to individual RSS feeds."""
    feed_urls = list(feeds)
    articles: list[Article] = []
    failures: list[SourceFailure] = []
    sources_ok = 0
    seen_urls: set[str] = set()
    for feed_url in feed_urls:
        try:
            feed = parser(feed_url)
        except Exception as exc:
            failures.append(
                SourceFailure(_safe_source_name(feed_url), type(exc).__name__)
            )
            continue

        entries = list(getattr(feed, "entries", []) or [])
        feed_metadata = getattr(feed, "feed", {}) or {}
        source = _safe_source_name(feed_metadata.get("title") or feed_url)
        status = getattr(feed, "status", None)
        http_error = isinstance(status, int) and status >= 400
        if not entries and (getattr(feed, "bozo", False) or http_error):
            parse_error = getattr(feed, "bozo_exception", None)
            if parse_error:
                error_type = type(parse_error).__name__
            elif http_error:
                error_type = "HTTPError"
            else:
                error_type = "FeedParseError"
            failures.append(SourceFailure(source, error_type))
            continue

        sources_ok += 1
        for entry in entries[:articles_per_feed]:
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

    result = SourceCollectionResult(
        articles=articles,
        sources_total=len(feed_urls),
        sources_ok=sources_ok,
        sources_failed=len(failures),
        failures=failures,
    )
    if feed_urls and sources_ok == 0:
        raise SourceCollectionError(result)
    return result


_READER_METADATA_PREFIXES = (
    "Title:",
    "URL Source:",
    "Published Time:",
    "Markdown Content:",
)
_MARKDOWN_IMAGE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
_MARKDOWN_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


def clean_extracted_text(raw: str, *, max_chars: int) -> str:
    """Deterministic cleanup before the model call: drop reader metadata and noise.

    This runs locally, costs no Gemini request and keeps only high-signal lines so
    the Writer receives dense context instead of raw reader output.
    """
    if not raw:
        return ""
    lines: list[str] = []
    header = True
    for raw_line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if header:
            if not stripped:
                continue
            if stripped.startswith(_READER_METADATA_PREFIXES):
                if stripped.startswith("Markdown Content:"):
                    header = False
                continue
            header = False
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _MARKDOWN_IMAGE.match(stripped) or _MARKDOWN_RULE.match(stripped):
            continue
        if lines and lines[-1] == stripped:
            continue
        lines.append(stripped)

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        boundary = cut.rfind("\n\n")
        if boundary > max_chars * 0.6:
            cut = cut[:boundary]
        text = cut.rstrip() + "\n[content truncated]"
    return text


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
