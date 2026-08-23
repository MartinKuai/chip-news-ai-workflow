"""Resilient batch orchestration around the per-article StateGraph."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any, TypedDict

from dotenv import load_dotenv

from .config import ConfigError, Settings
from .gemini import GeminiAPIError, GeminiResponseError
from .graph import NodeExecutionError, create_runtime_graph
from .publisher import PublisherError
from .schemas import Article, SchemaError
from .sources import (
    SourceCollectionError,
    SourceCollectionResult,
    SourceError,
    SourceFailure,
    collect_articles,
)


GEMINI_TRANSIENT_FAILURE_THRESHOLD = 2


class GlobalWorkflowError(RuntimeError):
    """A configuration or service failure that invalidates the whole run."""

    def __init__(self, stage: str, error_type: str) -> None:
        self.stage = stage
        self.error_type = error_type
        super().__init__(f"Global workflow failure at {stage}: {error_type}")


class FailureRecord(TypedDict):
    article: str
    stage: str
    error: str


def _safe_title(article: Article) -> str:
    """Keep summary output single-line and bounded without logging article bodies."""
    return " ".join(article.get("title", "Untitled article").split())[:160]


def _record_failure(
    failures: list[FailureRecord],
    article: Article,
    stage: str,
    cause: Exception,
) -> None:
    failures.append(
        {
            "article": _safe_title(article),
            "stage": stage,
            "error": type(cause).__name__,
        }
    )


def _is_global_failure(cause: Exception) -> bool:
    if isinstance(cause, GeminiAPIError):
        return cause.global_failure
    if isinstance(cause, PublisherError):
        return cause.global_failure
    if isinstance(cause, (GeminiResponseError, SourceError, SchemaError)):
        return False
    # Unknown exceptions are programming/runtime faults, not safe item failures.
    return True


def _print_summary(
    stats: dict[str, int],
    failures: list[FailureRecord],
    source_failures: list[SourceFailure],
    *,
    workflow_failed: bool,
) -> None:
    print("Run summary:")
    for name, value in stats.items():
        print(f"  {name}: {value}")
    print(f"  workflow_status: {'FAIL' if workflow_failed else 'PASS'}")
    if source_failures:
        print("Failed sources:")
        for failure in source_failures:
            print(f"  {failure.source} | error={failure.error}")
    if failures:
        print("Failed items:")
        for failure in failures:
            print(
                f"  {failure['article']} | stage={failure['stage']} "
                f"| error={failure['error']}"
            )


def run_daily(
    settings: Settings,
    *,
    graph: Any | None = None,
    articles: Iterable[Article] | None = None,
) -> dict[str, int]:
    """Process every independent item unless a run-wide failure is detected."""
    runtime_graph = graph or create_runtime_graph(settings)
    if articles is not None:
        collection = SourceCollectionResult(
            articles=list(articles),
            sources_total=0,
            sources_ok=0,
            sources_failed=0,
            failures=[],
        )
    else:
        try:
            collection = collect_articles(settings.articles_per_feed)
        except SourceCollectionError as exc:
            stats = {
                "sources_total": exc.result.sources_total,
                "sources_ok": exc.result.sources_ok,
                "sources_failed": exc.result.sources_failed,
                "candidates": 0,
                "processed": 0,
                "published": 0,
                "skipped": 0,
                "held": 0,
                "failed": 0,
                "revisions": 0,
            }
            _print_summary(
                stats,
                [],
                exc.result.failures,
                workflow_failed=True,
            )
            raise GlobalWorkflowError("sources", "SourceCollectionError") from None

    candidates = collection.articles
    stats = {
        "sources_total": collection.sources_total,
        "sources_ok": collection.sources_ok,
        "sources_failed": collection.sources_failed,
        "candidates": len(candidates),
        "processed": 0,
        "published": 0,
        "skipped": 0,
        "held": 0,
        "failed": 0,
        "revisions": 0,
    }
    failures: list[FailureRecord] = []
    fatal_failure: GlobalWorkflowError | None = None
    consecutive_gemini_transient_failures = 0

    for article in candidates:
        stats["processed"] += 1
        print(f"Processing: {_safe_title(article)}")
        try:
            result = runtime_graph.invoke(
                {
                    "article": article,
                    "revision_brief": [],
                    "revision_count": 0,
                    "status": "NEW",
                    "published": False,
                }
            )
        except NodeExecutionError as exc:
            cause = exc.cause
            stats["failed"] += 1
            _record_failure(failures, article, exc.stage, cause)

            if isinstance(cause, GeminiAPIError) and cause.transient:
                consecutive_gemini_transient_failures += 1
            elif (
                isinstance(cause, (GeminiResponseError, SchemaError))
                or exc.stage == "publisher"
            ):
                # Gemini responded; only this article's output or delivery was bad.
                consecutive_gemini_transient_failures = 0

            if _is_global_failure(cause):
                fatal_failure = GlobalWorkflowError(exc.stage, type(cause).__name__)
            elif (
                isinstance(cause, GeminiAPIError)
                and cause.transient
                and consecutive_gemini_transient_failures
                >= GEMINI_TRANSIENT_FAILURE_THRESHOLD
            ):
                fatal_failure = GlobalWorkflowError(
                    exc.stage, "GeminiServiceUnavailable"
                )

            if fatal_failure:
                break
            continue
        except Exception as exc:
            stats["failed"] += 1
            _record_failure(failures, article, "graph", exc)
            fatal_failure = GlobalWorkflowError("graph", type(exc).__name__)
            break

        # Any completed graph proves Gemini was available for this item.
        consecutive_gemini_transient_failures = 0
        status = result["status"]
        stats["revisions"] += int(result.get("revision_count", 0))
        if status == "SKIP":
            stats["skipped"] += 1
        elif status == "HOLD":
            stats["held"] += 1
        elif status == "PASS" and result.get("published"):
            stats["published"] += 1
        else:
            cause = RuntimeError("invalid terminal graph state")
            stats["failed"] += 1
            _record_failure(failures, article, "graph", cause)
            fatal_failure = GlobalWorkflowError("graph", type(cause).__name__)
            break

    _print_summary(
        stats,
        failures,
        collection.failures,
        workflow_failed=fatal_failure is not None,
    )
    if fatal_failure:
        raise fatal_failure from None
    return stats


def main() -> None:
    load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("Daily Chip News run started")
    try:
        run_daily(settings)
    except GlobalWorkflowError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
