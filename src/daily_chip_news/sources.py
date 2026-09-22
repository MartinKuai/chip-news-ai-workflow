"""RSS candidate collection and deterministic article extraction."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

_TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")
_JUNK_TITLE_SUBSTRINGS = (
    "advertisement",
    "sponsored",
    "we're hiring",
    "we are hiring",
    "job opening",
    "招聘",
    "广告",
    "newsletter",
)
_JUNK_TITLE_EXACT = frozenset(
    {
        "home",
        "about",
        "about us",
        "contact",
        "contact us",
        "subscribe",
        "newsletter",
        "sign in",
        "log in",
        "privacy policy",
        "terms of service",
        "rss",
    }
)
_JUNK_URL_PATTERNS = ("/jobs/", "/careers/", "/advertise", "/subscribe", "/newsletter")
_MIN_TITLE_LENGTH = 6


def canonical_url(url: str) -> str:
    """Normalize a URL for deterministic cross-source deduplication."""
    parsed = urlsplit((url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower().removeprefix("www.")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query_pairs)), ""))


def _recency_key(value: str) -> float:
    text = (value or "").strip()
    if not text:
        return 0.0
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _looks_like_junk(article: Article) -> bool:
    title = str(article.get("title", "")).strip().lower()
    url = str(article.get("url", "")).lower()
    if len(title) < _MIN_TITLE_LENGTH:
        return True
    if title in _JUNK_TITLE_EXACT:
        return True
    if any(pattern in title for pattern in _JUNK_TITLE_SUBSTRINGS):
        return True
    return any(pattern in url for pattern in _JUNK_URL_PATTERNS)


def select_candidates(
    articles: Iterable[Article],
    *,
    limit: int,
    max_per_source: int | None = None,
) -> list[Article]:
    """Deterministic, zero-Gemini candidate control.

    Canonical-URL dedupe, obvious junk removal, then round-robin source
    diversity with recency ordering inside each source.
    """
    if limit < 1:
        return []
    grouped: dict[str, list[Article]] = {}
    seen: set[str] = set()
    for article in articles:
        key = canonical_url(article.get("url", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        if _looks_like_junk(article):
            continue
        source = str(article.get("source", "")).strip() or "unknown"
        grouped.setdefault(source, []).append(article)

    queues: list[list[Article]] = []
    for items in grouped.values():
        items.sort(key=lambda item: _recency_key(item.get("published_at", "")), reverse=True)
        queues.append(items[:max_per_source] if max_per_source else items)

    selected: list[Article] = []
    while queues and len(selected) < limit:
        next_round: list[list[Article]] = []
        for queue in queues:
            if len(selected) >= limit:
                break
            selected.append(queue.pop(0))
            if queue:
                next_round.append(queue)
        queues = next_round
    return selected


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
