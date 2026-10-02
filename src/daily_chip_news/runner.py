"""Batch runner: candidate loop, run summary and notifications.

Pipeline: Sources -> Candidate Selection -> Researcher -> Writer -> Reviewer
(-> Revision) -> Publisher -> Run Summary / Notification.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv

from .config import ConfigError, Settings
from .errors import FailureCategory, classify_failure
from .graph import (
    STATUS_HOLD,
    STATUS_PUBLISHED,
    STATUS_REJECT,
    STATUS_SKIP,
    NodeExecutionError,
    create_runtime_graph,
)
from .health import ServiceHealth
from .metrics import RunMetrics
from .outcomes import CandidateStatus, RunOutcome, decide_run_outcome, exit_code_for
from .publisher import TelegramPublisher
from .schemas import Candidate
from .sources import (
    SourceCollectionError,
    SourceCollectionResult,
    collect_articles,
    select_candidates,
)

SKIP_NOT_RELEVANT = "researcher-skip"
SKIP_REJECTED = "reviewer-reject"
SKIP_REVISION_LIMIT = "revision-limit"


class GlobalWorkflowError(RuntimeError):
    """A run-level failure that must be reported through the summary and alert."""

    def __init__(
        self,
        stage: str,
        error_type: str,
        *,
        status_code: int | None = None,
        force_failed: bool = False,
    ) -> None:
        self.stage = stage
        self.error_type = error_type
        self.status_code = status_code
        self.force_failed = force_failed
        status_suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(
            f"Global workflow failure at {stage}: {error_type}{status_suffix}"
        )


class FailureRecord(TypedDict):
    candidate: str
    candidate_id: str
    source: str
    stage: str
    category: str
    error: str
    status_code: int | None


def _safe_title(candidate: Candidate) -> str:
    """Keep summary output single-line and bounded without logging article bodies."""
    return " ".join(candidate.get("title", "Untitled article").split())[:160]


def _safe_status_code(cause: Exception) -> int | None:
    """Return only a bounded HTTP status code; never expose provider messages."""
    status_code = getattr(cause, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return status_code
    return None


def _record_failure(
    failures: list[FailureRecord],
    candidate: Candidate,
    stage: str,
    cause: Exception,
    category: FailureCategory,
) -> None:
    failures.append(
        {
            "candidate": _safe_title(candidate),
            "candidate_id": str(candidate.get("id", "-")),
            "source": str(candidate.get("source", "-")),
            "stage": stage,
            "category": category.value,
            "error": type(cause).__name__,
            "status_code": _safe_status_code(cause),
        }
    )


def _failure_categories(failures: list[FailureRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for failure in failures:
        counts[failure["category"]] = counts.get(failure["category"], 0) + 1
    return counts


def _stage_failures(failures: list[FailureRecord], stage: str) -> int:
    return sum(1 for failure in failures if failure["stage"] == stage)


def _latest_status_code(failures: list[FailureRecord]) -> int | None:
    for failure in reversed(failures):
        if failure["status_code"] is not None:
            return failure["status_code"]
    return None


def _config_line(settings: Settings) -> str:
    return (
        "Daily Chip News config"
        f" | researcher_model={settings.researcher.model}"
        f" | writer_model={settings.writer.model}"
        f" | reviewer_model={settings.reviewer.model}"
        f" | thinking={settings.researcher.thinking_level or 'default'}"
        f"/{settings.writer.thinking_level or 'default'}"
        f"/{settings.reviewer.thinking_level or 'default'}"
        f" | max_output_tokens={settings.researcher.max_output_tokens}"
        f"/{settings.writer.max_output_tokens}"
        f"/{settings.reviewer.max_output_tokens}"
        f" | request_timeout={settings.gemini_timeout_seconds:.0f}s"
        f" | max_attempts={settings.gemini_max_attempts}"
        f" | call_budget={settings.gemini_call_budget_seconds:.0f}s"
        f" | fallback_models={','.join(settings.gemini_fallback_models) or 'none'}"
        f" | breaker={settings.breaker_failure_threshold}"
        f"/{settings.breaker_window_size}"
        f" | max_revisions={settings.max_revisions}"
        f" | run_budget={settings.run_budget_seconds:.0f}s"
        f" | sources={len(settings.source_feeds)}"
        f" | max_candidates={settings.max_candidates_per_run}"
        f" | max_age_hours={settings.max_candidate_age_hours:.0f}"
        f" | publish_enabled={'yes' if settings.publish_enabled else 'no'}"
    )


def _empty_stats() -> dict[str, int]:
    return {
        "sources_total": 0,
        "sources_ok": 0,
        "sources_failed": 0,
        "discovered": 0,
        "selected": 0,
        "processed": 0,
        "published": 0,
        "skipped": 0,
        "failed": 0,
        "revisions": 0,
        "skipped_not_relevant": 0,
        "skipped_rejected": 0,
        "skipped_revision_limit": 0,
    }


def _print_summary(
    settings: Settings,
    stats: dict[str, int],
    failures: list[FailureRecord],
    source_failures: list[Any],
    *,
    outcome: RunOutcome,
    metrics: RunMetrics,
    breaker: ServiceHealth,
    breaker_opened: bool,
    budget_exceeded: bool,
    node_success: dict[str, int] | None = None,
) -> None:
    categories = _failure_categories(failures)
    # Node counters are observed, never inferred from candidate outcomes: a
    # node that was never reached must not be reported as called.
    succeeded = node_success or {}

    def node_lines(stage: str) -> tuple[int, int, int]:
        ok = succeeded.get(stage, 0)
        failed = _stage_failures(failures, stage)
        return ok + failed, ok, failed

    print("Sources:")
    print(f"  total: {stats['sources_total']}")
    print(f"  successful: {stats['sources_ok']}")
    print(f"  failed: {stats['sources_failed']}")

    print("Run summary:")
    print(f"  run_outcome: {outcome.value}")
    print(f"  exit_code: {exit_code_for(outcome)}")
    print(f"  discovered: {stats['discovered']}")
    print(f"  selected: {stats['selected']}")
    print(f"  processed: {stats['processed']}")
    print(f"  published: {stats['published']}")
    print(f"  skipped: {stats['skipped']}")
    print(f"    researcher-skip: {stats['skipped_not_relevant']}")
    print(f"    reviewer-reject: {stats['skipped_rejected']}")
    print(f"    revision-limit: {stats['skipped_revision_limit']}")
    print(f"  failed: {stats['failed']}")
    print(f"  revisions: {stats['revisions']}")
    print(f"  breaker: {'open' if breaker_opened else 'closed'} | {breaker.snapshot()}")
    print(
        "  models: "
        f"researcher={settings.researcher.model} "
        f"writer={settings.writer.model} "
        f"reviewer={settings.reviewer.model}"
    )
    print(f"  workflow_status: {'FAIL' if outcome is RunOutcome.FAILED else 'PASS'}")
    if budget_exceeded:
        print(
            "  run_budget: exceeded"
            f" | processed={stats['processed']}/{stats['selected']}"
            " | remaining candidates were not processed"
        )
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
                f"  {failure['candidate_id']} | {failure['candidate']} "
                f"| source={failure['source']} | stage={failure['stage']} "
                f"| category={failure['category']} | error={failure['error']}"
                f"{status_suffix}"
            )
    if categories:
        print("  failure_categories:")
        for name in sorted(categories):
            print(f"    {name}: {categories[name]}")

    print("Gemini:")
    print(f"  requests: {metrics.gemini_requests}")
    print(f"  successful: {metrics.gemini_success}")
    print(f"  retries: {metrics.gemini_retries}")
    print(f"  transient_failures: {metrics.transient_failures}")
    print(f"  rate_limit_429: {metrics.transient_rate_limit}")
    print(f"  server_5xx: {metrics.transient_server}")
    print(f"  network: {metrics.transient_network}")
    print(f"  response_invalid: {metrics.response_invalid}")
    print(f"  response_truncated: {metrics.response_truncated}")
    print(f"  json_repairs: {sum(metrics.json_repairs.values())}")
    print(f"  thinking_downgrades: {metrics.thinking_downgrades}")
    print(f"  model_fallbacks: {metrics.model_fallbacks}")

    calls, ok, failed = node_lines("researcher")
    print("Researcher:")
    print(f"  calls: {calls}")
    print(f"  success: {ok}")
    print(f"  failures: {failed}")

    calls, ok, failed = node_lines("writer")
    print("Writer:")
    print(f"  calls: {calls}")
    print(f"  success: {ok}")
    print(f"  failures: {failed}")
    print(f"  json_repair_attempts: {metrics.repairs_for('writer')}")
    print(f"  json_repair_success: {metrics.repair_success_for('writer')}")

    calls, ok, failed = node_lines("reviewer")
    print("Reviewer:")
    print(f"  calls: {calls}")
    print(f"  success: {ok}")
    print(f"  rejected: {stats['skipped_rejected']}")
    print(f"  failures: {failed}")

    print("Publisher:")
    print(f"  published: {stats['published']}")
    print(f"  failures: {_stage_failures(failures, 'publisher')}")


def _alert_lines(
    title: str,
    outcome: RunOutcome,
    stats: dict[str, int],
    failures: list[FailureRecord],
    *,
    breaker_opened: bool,
    fatal_failure: GlobalWorkflowError | None,
    status_code: int | None,
) -> list[str]:
    lines = [
        title,
        f"Outcome: {outcome.value}",
    ]
    if fatal_failure is not None:
        lines.append(f"Stage: {fatal_failure.stage}")
        lines.append(f"Error: {fatal_failure.error_type}")
    lines.extend(
        [
            f"Published: {stats['published']}/{stats['selected']}",
            f"Processed: {stats['processed']}/{stats['selected']}",
            f"Failed: {stats['failed']}",
        ]
    )
    categories = _failure_categories(failures)
    if categories:
        summary = ", ".join(f"{name}={categories[name]}" for name in sorted(categories))
        lines.append(f"Categories: {summary}")
    if breaker_opened:
        lines.append("Breaker: open")
    if status_code is not None:
        lines.append(f"HTTP status: {status_code}")
    return lines


def _send_alert(
    alert_publisher: Callable[[str], None] | None,
    lines: list[str],
) -> None:
    """Best-effort deterministic alert delivery; this path never calls Gemini."""
    if alert_publisher is None:
        return
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


def _write_summary_file(
    path: str,
    payload: dict[str, Any],
    failures: list[FailureRecord],
    source_failures: list[Any],
) -> None:
    if not path:
        return
    document = {
        **payload,
        "failed_items": failures,
        "failed_sources": [
            {"source": failure.source, "error": failure.error}
            for failure in source_failures
        ],
    }
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Run summary written | path={target}")
    except OSError as exc:
        print(
            f"Run summary could not be written | error={type(exc).__name__}",
            file=sys.stderr,
        )


def run_daily(
    settings: Settings,
    *,
    graph: Any | None = None,
    candidates: Iterable[Candidate] | None = None,
    alert_publisher: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    breaker: ServiceHealth | None = None,
) -> dict[str, Any]:
    """Process every independent candidate and return stats plus the outcome."""
    print(_config_line(settings))
    metrics = RunMetrics()
    service_breaker = breaker or ServiceHealth(
        settings.breaker_window_size, settings.breaker_failure_threshold
    )
    deadline = clock() + settings.run_budget_seconds
    current: dict[str, str] = {"id": "-", "title": "-", "source": "-"}
    node_success: dict[str, int] = {}

    def on_step(stage: str, status: str) -> None:
        node_success[stage] = node_success.get(stage, 0) + 1
        print(f"Candidate step | id={current['id']} | node={stage} | status={status}")

    runtime_graph = (
        graph
        if graph is not None
        else create_runtime_graph(
            settings,
            metrics=metrics,
            breaker=service_breaker,
            run_deadline=deadline,
            on_step=on_step,
        )
    )

    if candidates is not None:
        collection = SourceCollectionResult(
            candidates=list(candidates),
            sources_total=0,
            sources_ok=0,
            sources_failed=0,
            failures=[],
        )
    else:
        try:
            collection = collect_articles(
                settings.articles_per_feed, feeds=settings.source_feeds
            )
        except SourceCollectionError as exc:
            stats = _empty_stats()
            stats["sources_total"] = exc.result.sources_total
            stats["sources_ok"] = exc.result.sources_ok
            stats["sources_failed"] = exc.result.sources_failed
            fatal_failure = GlobalWorkflowError(
                "sources", "SourceCollectionError", force_failed=True
            )
            print("Run outcome decision | reason=all-sources-unavailable -> FAILED")
            _print_summary(
                settings,
                stats,
                [],
                exc.result.failures,
                outcome=RunOutcome.FAILED,
                metrics=metrics,
                breaker=service_breaker,
                breaker_opened=False,
                budget_exceeded=False,
            )
            _send_alert(
                alert_publisher,
                _alert_lines(
                    "⚠️ Daily Chip News 运行失败",
                    RunOutcome.FAILED,
                    stats,
                    [],
                    breaker_opened=False,
                    fatal_failure=fatal_failure,
                    status_code=None,
                ),
            )
            result = {
                **stats,
                "run_outcome": RunOutcome.FAILED.value,
                "exit_code": exit_code_for(RunOutcome.FAILED),
            }
            _write_summary_file(settings.summary_path, result, [], exc.result.failures)
            return result

    selected = select_candidates(
        collection.candidates,
        limit=settings.max_candidates_per_run,
        max_age_hours=settings.max_candidate_age_hours,
    )
    print(
        "Candidate control | "
        f"discovered={len(collection.candidates)} "
        f"| selected={len(selected)} "
        f"| limit={settings.max_candidates_per_run} "
        f"| max_age_hours={settings.max_candidate_age_hours:.0f}"
    )
    stats = {
        **_empty_stats(),
        "sources_total": collection.sources_total,
        "sources_ok": collection.sources_ok,
        "sources_failed": collection.sources_failed,
        "discovered": len(collection.candidates),
        "selected": len(selected),
    }
    failures: list[FailureRecord] = []
    fatal_failure: GlobalWorkflowError | None = None
    breaker_opened = False
    budget_exceeded = False
    started = clock()

    for candidate in selected:
        if clock() >= deadline:
            budget_exceeded = True
            print(
                "Run budget reached | "
                f"elapsed={clock() - started:.0f}s | processed="
                f"{stats['processed']}/{stats['selected']} | action=stop_processing"
            )
            break
        current["id"] = str(candidate.get("id", "-"))
        current["title"] = _safe_title(candidate)
        current["source"] = str(candidate.get("source", "-"))
        stats["processed"] += 1
        print(
            f"Candidate start | id={current['id']} | source={current['source']} "
            f"| title={current['title']}"
        )
        try:
            result = runtime_graph.invoke(
                {
                    "candidate": candidate,
                    "revision_brief": [],
                    "revision_count": 0,
                    "status": "NEW",
                    "published": False,
                }
            )
        except NodeExecutionError as exc:
            cause = exc.cause
            info = classify_failure(exc.stage, cause)
            stats["failed"] += 1
            _record_failure(failures, candidate, exc.stage, cause, info.category)
            status_code = _safe_status_code(cause)
            status_suffix = (
                f" | status_code={status_code}" if status_code is not None else ""
            )
            print(
                f"Candidate failed | id={current['id']} | node={exc.stage} "
                f"| category={info.category.value} | error={type(cause).__name__}"
                f"{status_suffix}"
            )
            if info.stop_run:
                fatal_failure = GlobalWorkflowError(
                    exc.stage,
                    type(cause).__name__,
                    status_code=status_code,
                    force_failed=info.force_failed,
                )
                print(
                    "Run outcome decision | reason=stop-run-failure "
                    f"| stage={exc.stage} | category={info.category.value}"
                )
                break
            if service_breaker.is_open:
                breaker_opened = True
                print(
                    "Gemini service breaker OPEN | "
                    f"{service_breaker.snapshot()} | last=stage={exc.stage} "
                    f"category={info.category.value} "
                    f"| published={stats['published']} "
                    "| action=stop_processing"
                )
                break
            continue
        except Exception as exc:
            stats["failed"] += 1
            _record_failure(
                failures,
                candidate,
                "graph",
                exc,
                FailureCategory.UNEXPECTED_ERROR,
            )
            fatal_failure = GlobalWorkflowError(
                "graph", type(exc).__name__, force_failed=True
            )
            print("Run outcome decision | reason=unexpected-exception -> FAILED")
            break

        status = str(result.get("status", ""))
        stats["revisions"] += int(result.get("revision_count", 0))
        if status == STATUS_PUBLISHED and result.get("published"):
            stats["published"] += 1
            final_status = CandidateStatus.PUBLISHED.value
            reason = "published"
        elif status == STATUS_SKIP:
            stats["skipped"] += 1
            stats["skipped_not_relevant"] += 1
            final_status = CandidateStatus.SKIPPED.value
            reason = SKIP_NOT_RELEVANT
        elif status == STATUS_REJECT:
            stats["skipped"] += 1
            stats["skipped_rejected"] += 1
            final_status = CandidateStatus.SKIPPED.value
            reason = SKIP_REJECTED
        elif status == STATUS_HOLD:
            stats["skipped"] += 1
            stats["skipped_revision_limit"] += 1
            final_status = CandidateStatus.SKIPPED.value
            reason = SKIP_REVISION_LIMIT
        else:
            cause = RuntimeError("invalid terminal graph state")
            stats["failed"] += 1
            _record_failure(
                failures,
                candidate,
                "graph",
                cause,
                FailureCategory.UNEXPECTED_ERROR,
            )
            fatal_failure = GlobalWorkflowError(
                "graph", type(cause).__name__, force_failed=True
            )
            print("Run outcome decision | reason=invalid-terminal-state -> FAILED")
            break
        print(
            f"Candidate done | id={current['id']} | status={final_status} "
            f"| reason={reason} | revisions={int(result.get('revision_count', 0))} "
            f"| title={current['title']}"
        )

    outcome = decide_run_outcome(
        published=stats["published"],
        failed=stats["failed"],
        stopped_early=breaker_opened or budget_exceeded,
        force_failed=bool(fatal_failure and fatal_failure.force_failed),
    )
    print(
        "Run outcome decision | "
        f"published={stats['published']} | failed={stats['failed']} "
        f"| skipped={stats['skipped']} "
        f"| breaker={'open' if breaker_opened else 'closed'} "
        f"| budget_exceeded={'yes' if budget_exceeded else 'no'} "
        f"| force_failed="
        f"{'yes' if fatal_failure and fatal_failure.force_failed else 'no'} "
        f"-> {outcome.value} (exit {exit_code_for(outcome)})"
    )
    _print_summary(
        settings,
        stats,
        failures,
        collection.failures,
        outcome=outcome,
        metrics=metrics,
        breaker=service_breaker,
        breaker_opened=breaker_opened,
        budget_exceeded=budget_exceeded,
        node_success=node_success,
    )
    result = {
        **stats,
        "run_outcome": outcome.value,
        "exit_code": exit_code_for(outcome),
        "researcher_model": settings.researcher.model,
        "writer_model": settings.writer.model,
        "reviewer_model": settings.reviewer.model,
        "breaker_opened": breaker_opened,
    }
    if outcome is RunOutcome.FAILED:
        failure = fatal_failure or GlobalWorkflowError(
            "summary",
            failures[-1]["category"] if failures else "NoCandidatePublished",
            status_code=_latest_status_code(failures),
        )
        _send_alert(
            alert_publisher,
            _alert_lines(
                "⚠️ Daily Chip News 运行失败",
                outcome,
                stats,
                failures,
                breaker_opened=breaker_opened,
                fatal_failure=failure,
                status_code=failure.status_code or _latest_status_code(failures),
            ),
        )
    elif outcome is RunOutcome.PARTIAL_SUCCESS:
        _send_alert(
            alert_publisher,
            _alert_lines(
                "⚠️ Daily Chip News 部分成功",
                outcome,
                stats,
                failures,
                breaker_opened=breaker_opened,
                fatal_failure=None,
                status_code=_latest_status_code(failures),
            ),
        )
    _write_summary_file(settings.summary_path, result, failures, collection.failures)
    return result


def main() -> None:
    load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("Daily Chip News run started")
    alert_publisher = TelegramPublisher(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        timeout=settings.telegram_timeout_seconds,
        enabled=settings.publish_enabled,
    ).publish_alert
    result = run_daily(settings, alert_publisher=alert_publisher)
    raise SystemExit(int(result["exit_code"]))
