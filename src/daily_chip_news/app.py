"""Resilient batch orchestration around the per-article StateGraph."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from typing import Any, TypedDict

from dotenv import load_dotenv

from .config import ConfigError, Settings
from .gemini import GeminiAPIError, GeminiResponseError
from .graph import NodeExecutionError, create_runtime_graph
from .publisher import PublisherError, TelegramPublisher
from .schemas import Article, SchemaError
from .sources import (
    SourceCollectionError,
    SourceCollectionResult,
    SourceError,
    SourceFailure,
    collect_articles,
)


GEMINI_TRANSIENT_FAILURE_THRESHOLD = 2
AI_STAGES = frozenset({"researcher", "writer", "reviewer"})


class GlobalWorkflowError(RuntimeError):
    """A configuration or service failure that invalidates the whole run."""

    def __init__(
        self,
        stage: str,
        error_type: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.stage = stage
        self.error_type = error_type
        self.status_code = status_code
        status_suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(
            f"Global workflow failure at {stage}: {error_type}{status_suffix}"
        )


class FailureRecord(TypedDict):
    article: str
    stage: str
    error: str
    status_code: int | None


def _safe_title(article: Article) -> str:
    """Keep summary output single-line and bounded without logging article bodies."""
    return " ".join(article.get("title", "Untitled article").split())[:160]


def _safe_status_code(cause: Exception) -> int | None:
    """Return only a bounded HTTP status code; never expose provider messages."""
    status_code = getattr(cause, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return status_code
    return None


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
            "status_code": _safe_status_code(cause),
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
            status_suffix = (
                f" | status_code={failure['status_code']}"
                if failure["status_code"] is not None
                else ""
            )
            print(
                f"  {failure['article']} | stage={failure['stage']} "
                f"| error={failure['error']}{status_suffix}"
            )


def _latest_status_code(failures: list[FailureRecord]) -> int | None:
    for failure in reversed(failures):
        if failure["status_code"] is not None:
            return failure["status_code"]
    return None


def _send_ops_alert(
    alert_publisher: Callable[[str], None] | None,
    stats: dict[str, int],
    failure: GlobalWorkflowError,
    failures: list[FailureRecord],
) -> None:
    """Best-effort deterministic alert delivery; this path never calls Gemini."""
    if alert_publisher is None:
        return

    status_code = failure.status_code or _latest_status_code(failures)
    lines = [
        "⚠️ Daily Chip News 运行失败",
        f"Stage: {failure.stage}",
        f"Error: {failure.error_type}",
        f"Published: {stats['published']}/{stats['candidates']}",
        f"Processed: {stats['processed']}/{stats['candidates']}",
        f"Failed: {stats['failed']}",
    ]
    if failure.stage == "summary" and failures:
        lines.append(f"Last item stage: {failures[-1]['stage']}")
    if status_code is not None:
        lines.append(f"HTTP status: {status_code}")

    try:
        alert_publisher("\n".join(lines))
    except Exception as exc:
        # A failed alert must not hide the original workflow failure. Log only
        # the safe exception type and optional HTTP status.
        alert_status = _safe_status_code(exc)
        status_suffix = (
            f" | status_code={alert_status}" if alert_status is not None else ""
        )
        print(
            f"Operations alert failed | error={type(exc).__name__}{status_suffix}",
            file=sys.stderr,
        )


def run_daily(
    settings: Settings,
    *,
    graph: Any | None = None,
    articles: Iterable[Article] | None = None,
    alert_publisher: Callable[[str], None] | None = None,
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
            fatal_failure = GlobalWorkflowError("sources", "SourceCollectionError")
            _send_ops_alert(alert_publisher, stats, fatal_failure, [])
            raise fatal_failure from None

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

            is_ai_transient_failure = (
                isinstance(cause, GeminiAPIError)
                and cause.transient
                and exc.stage in AI_STAGES
            )
            if is_ai_transient_failure:
                consecutive_gemini_transient_failures += 1
            else:
                # Only transient Gemini failures in the three AI stages count;
                # every other item outcome breaks the consecutive sequence.
                consecutive_gemini_transient_failures = 0

            if _is_global_failure(cause):
                fatal_failure = GlobalWorkflowError(
                    exc.stage,
                    type(cause).__name__,
                    status_code=_safe_status_code(cause),
                )
            elif (
                is_ai_transient_failure
                and consecutive_gemini_transient_failures
                >= GEMINI_TRANSIENT_FAILURE_THRESHOLD
            ):
                fatal_failure = GlobalWorkflowError(
                    exc.stage,
                    "GeminiServiceUnavailable",
                    status_code=_safe_status_code(cause),
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

    if fatal_failure is None and stats["published"] == 0 and stats["failed"] > 0:
        fatal_failure = GlobalWorkflowError(
            "summary",
            "NoArticlesPublished",
            status_code=_latest_status_code(failures),
        )

    _print_summary(
        stats,
        failures,
        collection.failures,
        workflow_failed=fatal_failure is not None,
    )
    if fatal_failure:
        _send_ops_alert(alert_publisher, stats, fatal_failure, failures)
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
        alert_publisher = TelegramPublisher(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        ).publish_alert
        run_daily(settings, alert_publisher=alert_publisher)
    except GlobalWorkflowError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
