"""Resilient batch orchestration around the per-article StateGraph."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv

from .config import ConfigError, Settings
from .cost_guard import CostGuard, CostGuardExceeded
from .errors import FailureCategory, classify_failure
from .graph import NodeExecutionError, create_runtime_graph
from .health import ServiceHealth
from .ledger import CostLedger, LedgerCorruptError, LedgerReadError
from .metrics import RunMetrics
from .outcomes import RunOutcome, decide_run_outcome, exit_code_for
from .publisher import TelegramPublisher
from .schemas import Article
from .sources import (
    SourceCollectionError,
    SourceCollectionResult,
    collect_articles,
    select_candidates,
)


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
    article: str
    stage: str
    category: str
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
    category: FailureCategory,
) -> None:
    failures.append(
        {
            "article": _safe_title(article),
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


def _usage_line(usage: Any) -> str:
    return (
        f"prompt={usage.prompt_tokens} candidate={usage.candidate_tokens} "
        f"thought={usage.thought_tokens} requests={usage.billable_requests} "
        f"cost_usd={usage.cost_usd:.6f}"
    )


def _latest_status_code(failures: list[FailureRecord]) -> int | None:
    for failure in reversed(failures):
        if failure["status_code"] is not None:
            return failure["status_code"]
    return None


def _config_line(settings: Settings) -> str:
    return (
        "Daily Chip News config"
        f" | researcher_model={settings.researcher_model}"
        f" | writer_model={settings.writer_model}"
        f" | reviewer_model={settings.reviewer_model}"
        f" | researcher_thinking={settings.researcher_thinking_level or 'default'}"
        f" | writer_thinking={settings.writer_thinking_level or 'default'}"
        f" | reviewer_thinking={settings.reviewer_thinking_level or 'default'}"
        f" | max_output_tokens={settings.researcher_max_output_tokens}"
        f"/{settings.writer_max_output_tokens}"
        f"/{settings.reviewer_max_output_tokens}"
        f" | request_timeout={settings.gemini_timeout_seconds:.0f}s"
        f" | max_attempts={settings.gemini_max_attempts}"
        f" | call_budget={settings.gemini_call_budget_seconds:.0f}s"
        f" | health_window={settings.health_window_size}"
        f"/{settings.health_failure_threshold}"
        f" | max_revisions={settings.max_revisions}"
        f" | run_budget={settings.run_budget_seconds:.0f}s"
        f" | run_mode={settings.run_mode}"
        f" | run_budget_usd={settings.run_budget_usd:.4f}"
        f" | rolling_30d_budget_usd={settings.gemini_rolling_30d_budget_usd:.2f}"
    )


def _print_summary(
    stats: dict[str, int],
    failures: list[FailureRecord],
    source_failures: list[Any],
    *,
    outcome: RunOutcome,
    metrics: RunMetrics,
    health: ServiceHealth,
    cost_guard: CostGuard,
    ledger: CostLedger | None,
    breaker_opened: bool,
    budget_exceeded: bool,
) -> None:
    categories = _failure_categories(failures)
    researcher_failures = _stage_failures(failures, "researcher")
    writer_failures = _stage_failures(failures, "writer")
    reviewer_failures = _stage_failures(failures, "reviewer")
    writer_calls = stats["processed"] + stats["revisions"]
    reviewer_calls = stats["published"] + stats["held"] + stats["revisions"]

    print("Run summary:")
    print(f"  run_outcome: {outcome.value}")
    print(f"  exit_code: {exit_code_for(outcome)}")
    print(f"  sources_total: {stats['sources_total']}")
    print(f"  sources_ok: {stats['sources_ok']}")
    print(f"  sources_failed: {stats['sources_failed']}")
    print(f"  discovered: {stats['discovered']}")
    print(f"  selected: {stats['selected']}")
    print(f"  candidates: {stats['candidates']}")
    print(f"  processed: {stats['processed']}")
    print(f"  published: {stats['published']}")
    print(f"  skipped: {stats['skipped']}")
    print(f"  held: {stats['held']}")
    print(f"  failed: {stats['failed']}")
    print(f"  revisions: {stats['revisions']}")
    print(f"  workflow_status: {'FAIL' if outcome is RunOutcome.FAILED else 'PASS'}")
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
                f"| category={failure['category']} | error={failure['error']}"
                f"{status_suffix}"
            )
    if budget_exceeded:
        print(
            "Run budget exceeded | remaining articles were not processed"
            f" | processed={stats['processed']}/{stats['candidates']}"
        )

    print("Sources:")
    print(f"  total: {stats['sources_total']}")
    print(f"  successful: {stats['sources_ok']}")
    print(f"  failed: {stats['sources_failed']}")

    print("Articles:")
    print(f"  discovered: {stats['discovered']}")
    print(f"  selected: {stats['selected']}")
    print(f"  eligible: {stats['eligible']}")
    print(f"  processed: {stats['processed']}")
    print(f"  published: {stats['published']}")
    print(f"  skipped: {stats['skipped']}")
    print(f"  held: {stats['held']}")
    print(f"  failed: {stats['failed']}")
    if categories:
        print("  failure_categories:")
        for name in sorted(categories):
            print(f"    {name}: {categories[name]}")

    print("Gemini:")
    print(f"  requests: {metrics.gemini_requests}")
    print(f"  successful: {metrics.gemini_success}")
    print(f"  retries: {metrics.gemini_retries}")
    print(
        "  transient_failures: "
        f"{metrics.transient_rate_limit + metrics.transient_server + metrics.transient_network}"
    )
    print(f"  rate_limit_429: {metrics.transient_rate_limit}")
    print(f"  server_5xx: {metrics.transient_server}")
    print(f"  network: {metrics.transient_network}")
    print(f"  response_invalid: {metrics.response_invalid}")
    print(f"  response_truncated: {metrics.response_truncated}")
    print(f"  thinking_downgrades: {metrics.thinking_downgrades}")
    print(f"  breaker_triggered: {'yes' if breaker_opened else 'no'}")
    print(f"  health_window: {health.snapshot()}")

    print("Gemini Cost:")
    print(f"  prompt_tokens: {metrics.gemini_usage.prompt_tokens}")
    print(f"  candidate_tokens: {metrics.gemini_usage.candidate_tokens}")
    print(f"  thought_tokens: {metrics.gemini_usage.thought_tokens}")
    print(f"  total_tokens: {metrics.gemini_usage.total_tokens}")
    print(
        "  successful_billable_requests: "
        f"{metrics.gemini_usage.billable_requests}"
    )
    print(f"  estimated_cost_usd: {metrics.gemini_usage.cost_usd:.6f}")
    print(f"  run_budget_usd: {cost_guard.run_budget_usd:.6f}")
    print(f"  run_spend_usd: {cost_guard.run_spend_usd:.6f}")
    print(f"  remaining_run_budget_usd: {cost_guard.remaining_run_budget_usd:.6f}")
    rolling_spend = (
        ledger.rolling_spend_usd()
        if ledger is not None
        else cost_guard.rolling_spend_usd
    )
    print(f"  rolling_30d_spend_usd: {rolling_spend:.6f}")
    print(f"  rolling_30d_budget_usd: {cost_guard.rolling_budget_usd:.2f}")
    print(
        "  remaining_30d_budget_usd: "
        f"{max(0.0, cost_guard.rolling_budget_usd - rolling_spend):.6f}"
    )
    print(
        f"  cost_guard_triggered: {'yes' if cost_guard.state.triggered else 'no'}"
    )
    print(f"  cost_guard_reason: {cost_guard.state.reason or 'none'}")
    print(f"  usage_missing_responses: {metrics.usage_missing_responses}")
    print(f"  pricing_unknown_requests: {metrics.pricing_unknown_requests}")

    print("Researcher:")
    print(f"  calls: {stats['processed']}")
    print(f"  success: {max(0, stats['processed'] - researcher_failures)}")
    print(f"  failures: {researcher_failures}")
    print(f"  usage: {_usage_line(metrics.usage_for('researcher'))}")

    print("Writer:")
    print(f"  calls: {writer_calls}")
    print(f"  success: {max(0, writer_calls - writer_failures)}")
    print(f"  skipped: {stats['skipped']}")
    print(f"  failures: {writer_failures}")
    print(f"  json_repair_attempts: {metrics.repairs_for('writer')}")
    print(f"  json_repair_success: {metrics.repair_success_for('writer')}")
    print(f"  usage: {_usage_line(metrics.usage_for('writer'))}")

    print("Reviewer:")
    print(f"  calls: {reviewer_calls}")
    print(f"  success: {max(0, reviewer_calls - reviewer_failures)}")
    print(f"  rejected: {stats['held']}")
    print(f"  failures: {reviewer_failures}")
    print(f"  usage: {_usage_line(metrics.usage_for('reviewer'))}")


def _alert_lines(
    title: str,
    outcome: RunOutcome,
    stats: dict[str, int],
    failures: list[FailureRecord],
    *,
    breaker_opened: bool,
    fatal_failure: GlobalWorkflowError | None,
    status_code: int | None,
    cost_guard_reason: str | None = None,
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
            f"Published: {stats['published']}/{stats['candidates']}",
            f"Processed: {stats['processed']}/{stats['candidates']}",
            f"Failed: {stats['failed']}",
        ]
    )
    categories = _failure_categories(failures)
    if categories:
        summary = ", ".join(f"{name}={categories[name]}" for name in sorted(categories))
        lines.append(f"Categories: {summary}")
    if breaker_opened:
        lines.append("Breaker: open")
    if cost_guard_reason:
        lines.append(f"Cost guard: {cost_guard_reason}")
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


def _final_result(stats: dict[str, int], outcome: RunOutcome) -> dict[str, Any]:
    return {
        **stats,
        "run_outcome": outcome.value,
        "exit_code": exit_code_for(outcome),
    }


def _empty_stats() -> dict[str, int]:
    return {
        "sources_total": 0,
        "sources_ok": 0,
        "sources_failed": 0,
        "discovered": 0,
        "selected": 0,
        "candidates": 0,
        "eligible": 0,
        "processed": 0,
        "published": 0,
        "skipped": 0,
        "held": 0,
        "failed": 0,
        "revisions": 0,
    }


def run_daily(
    settings: Settings,
    *,
    graph: Any | None = None,
    articles: Iterable[Article] | None = None,
    alert_publisher: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    health: ServiceHealth | None = None,
    cost_guard: CostGuard | None = None,
) -> dict[str, Any]:
    """Process every independent item and return stats plus the run outcome."""
    print(_config_line(settings))
    metrics = RunMetrics()
    health = health or ServiceHealth(
        settings.health_window_size, settings.health_failure_threshold
    )

    # --- rolling cost ledger (fail closed when it cannot be trusted) -------
    ledger_path_raw = os.environ.get("COST_LEDGER_PATH", "").strip()
    ledger_required = os.environ.get("COST_LEDGER_REQUIRED", "0").strip() == "1"
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    ledger: CostLedger | None = None
    ledger_error = ""
    ledger_path: Path | None = None
    if ledger_path_raw:
        ledger_path = Path(ledger_path_raw)
        try:
            if ledger_path.exists():
                ledger = CostLedger.load(ledger_path)
                print(
                    "Cost ledger loaded | "
                    f"entries={len(ledger.entries)} "
                    f"| rolling_30d_spend_usd={ledger.rolling_spend_usd():.6f}"
                )
            elif ledger_required:
                # The prepare step always creates the ledger; a missing file in
                # CI means artifact storage lost it, so fail closed.
                raise LedgerReadError(
                    f"required cost ledger is missing at {ledger_path}"
                )
            else:
                ledger = CostLedger.empty()
                print("Cost ledger initialized | reason=no ledger file yet")
        except (LedgerCorruptError, LedgerReadError) as exc:
            ledger = None
            ledger_error = str(exc)
    elif ledger_required:
        ledger_error = "COST_LEDGER_REQUIRED=1 but COST_LEDGER_PATH is not set"

    if ledger_error:
        print(
            f"Cost ledger unavailable: {ledger_error} | action=fail_closed",
            file=sys.stderr,
        )
        stats = _empty_stats()
        fatal_failure = GlobalWorkflowError(
            "ledger", "LedgerUnavailable", force_failed=True
        )
        report_guard = CostGuard(
            run_budget_usd=settings.run_budget_usd,
            rolling_budget_usd=settings.gemini_rolling_30d_budget_usd,
        )
        print("Run outcome decision | reason=cost-ledger-unavailable -> FAILED")
        _print_summary(
            stats,
            [],
            [],
            outcome=RunOutcome.FAILED,
            metrics=metrics,
            health=health,
            cost_guard=report_guard,
            ledger=None,
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
        return _final_result(stats, RunOutcome.FAILED)

    def record_cost_event(event, reservation, usage) -> None:
        if ledger is None or ledger_path is None:
            return
        ledger.record_guard_event(
            event, reservation, usage, run_id=run_id, mode=settings.run_mode
        )
        ledger.save(ledger_path)

    guard = cost_guard or CostGuard(
        run_budget_usd=settings.run_budget_usd,
        rolling_budget_usd=settings.gemini_rolling_30d_budget_usd,
        rolling_spend_usd=ledger.rolling_spend_usd() if ledger is not None else 0.0,
        recorder=record_cost_event if ledger is not None else None,
    )

    def finalize_ledger() -> None:
        if ledger is None or ledger_path is None:
            return
        ledger.resolve_run_open(run_id=run_id, actual_cost_usd=guard.run_spend_usd)
        ledger.save(ledger_path)
        print(
            "Cost ledger finalized | "
            f"run_id={run_id} | run_spend_usd={guard.run_spend_usd:.6f} "
            f"| rolling_30d_spend_usd={ledger.rolling_spend_usd():.6f}"
        )

    deadline = clock() + settings.run_budget_seconds
    runtime_graph = (
        graph
        if graph is not None
        else create_runtime_graph(
            settings,
            metrics=metrics,
            health=health,
            cost_guard=guard,
            run_deadline=deadline,
        )
    )

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
            stats = _empty_stats()
            stats.update(
                {
                    "sources_total": exc.result.sources_total,
                    "sources_ok": exc.result.sources_ok,
                    "sources_failed": exc.result.sources_failed,
                }
            )
            fatal_failure = GlobalWorkflowError(
                "sources", "SourceCollectionError", force_failed=True
            )
            finalize_ledger()
            print("Run outcome decision | reason=all-sources-unavailable -> FAILED")
            _print_summary(
                stats,
                [],
                exc.result.failures,
                outcome=RunOutcome.FAILED,
                metrics=metrics,
                health=health,
                cost_guard=guard,
                ledger=ledger,
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
            return _final_result(stats, RunOutcome.FAILED)

    candidates = select_candidates(
        collection.articles, limit=settings.max_candidates_per_run
    )
    print(
        "Candidate control | "
        f"discovered={len(collection.articles)} "
        f"| selected={len(candidates)} "
        f"| limit={settings.max_candidates_per_run}"
    )
    stats = {
        "sources_total": collection.sources_total,
        "sources_ok": collection.sources_ok,
        "sources_failed": collection.sources_failed,
        "discovered": len(collection.articles),
        "selected": len(candidates),
        "candidates": len(candidates),
        "eligible": 0,
        "processed": 0,
        "published": 0,
        "skipped": 0,
        "held": 0,
        "failed": 0,
        "revisions": 0,
    }
    failures: list[FailureRecord] = []
    fatal_failure: GlobalWorkflowError | None = None
    breaker_opened = False
    budget_exceeded = False
    cost_guard_stopped = False
    started = clock()

    for article in candidates:
        if clock() >= deadline:
            budget_exceeded = True
            print(
                "Run budget reached | "
                f"elapsed={clock() - started:.0f}s"
                f" | processed={stats['processed']}/{stats['candidates']}"
                " | action=stop_processing"
            )
            break
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
            info = classify_failure(exc.stage, cause)
            stats["failed"] += 1
            _record_failure(failures, article, exc.stage, cause, info.category)
            # Node-level code records the health event for the failing AI call.
            status_code = _safe_status_code(cause)
            status_suffix = (
                f" | status_code={status_code}" if status_code is not None else ""
            )
            print(
                f"Article failure | {_safe_title(article)} | stage={exc.stage} "
                f"| category={info.category.value} | error={type(cause).__name__}"
                f"{status_suffix}"
            )
            if info.stop_run:
                if info.category is FailureCategory.COST_GUARD:
                    cost_guard_stopped = True
                    if not guard.state.triggered and isinstance(
                        cause, CostGuardExceeded
                    ):
                        guard.state.triggered = True
                        guard.state.reason = cause.reason.value
                        guard.state.details = dict(cause.details)
                fatal_failure = GlobalWorkflowError(
                    exc.stage,
                    type(cause).__name__,
                    status_code=status_code,
                    force_failed=info.force_failed,
                )
                if cost_guard_stopped:
                    print(
                        "Cost guard stopped the run | "
                        f"reason={guard.state.reason or 'unknown'} "
                        f"| details={guard.state.details} "
                        f"| run_spend_usd={guard.run_spend_usd:.6f} "
                        f"| rolling_30d_spend_usd={guard.rolling_spend_usd:.6f} "
                        f"| published={stats['published']} "
                        "| action=stop_processing"
                    )
                else:
                    print(
                        "Run outcome decision | reason=stop-run-failure "
                        f"| stage={exc.stage} | category={info.category.value}"
                    )
                break
            if health.is_open:
                breaker_opened = True
                print(
                    "Gemini service breaker OPEN | "
                    f"{health.snapshot()} | last=stage={exc.stage} "
                    f"category={info.category.value} | published={stats['published']} "
                    "| action=stop_processing"
                )
                break
            continue
        except Exception as exc:
            stats["failed"] += 1
            _record_failure(
                failures,
                article,
                "graph",
                exc,
                FailureCategory.UNEXPECTED_ERROR,
            )
            fatal_failure = GlobalWorkflowError(
                "graph", type(exc).__name__, force_failed=True
            )
            print("Run outcome decision | reason=unexpected-exception -> FAILED")
            break

        # Node-level code already recorded one health event per AI call made
        # inside this article; the batch runner only aggregates the result.
        status = result["status"]
        stats["revisions"] += int(result.get("revision_count", 0))
        if status == "SKIP":
            stats["skipped"] += 1
        elif status == "HOLD":
            stats["eligible"] += 1
            stats["held"] += 1
        elif status == "PASS" and result.get("published"):
            stats["eligible"] += 1
            stats["published"] += 1
        else:
            cause = RuntimeError("invalid terminal graph state")
            stats["failed"] += 1
            _record_failure(
                failures,
                article,
                "graph",
                cause,
                FailureCategory.UNEXPECTED_ERROR,
            )
            fatal_failure = GlobalWorkflowError(
                "graph", type(cause).__name__, force_failed=True
            )
            print("Run outcome decision | reason=invalid-terminal-state -> FAILED")
            break

    outcome = decide_run_outcome(
        published=stats["published"],
        failed=stats["failed"],
        breaker_opened=breaker_opened,
        budget_exceeded=budget_exceeded,
        force_failed=bool(fatal_failure and fatal_failure.force_failed),
        cost_guard_stopped=cost_guard_stopped,
    )
    print(
        "Run outcome decision | "
        f"published={stats['published']} | failed={stats['failed']} "
        f"| breaker={'open' if breaker_opened else 'closed'} "
        f"| budget_exceeded={'yes' if budget_exceeded else 'no'} "
        f"| cost_guard_stopped={'yes' if cost_guard_stopped else 'no'} "
        f"| force_failed={'yes' if fatal_failure and fatal_failure.force_failed else 'no'} "
        f"-> {outcome.value} (exit {exit_code_for(outcome)})"
    )
    finalize_ledger()
    _print_summary(
        stats,
        failures,
        collection.failures,
        outcome=outcome,
        metrics=metrics,
        health=health,
        cost_guard=guard,
        ledger=ledger,
        breaker_opened=breaker_opened,
        budget_exceeded=budget_exceeded,
    )
    if outcome is RunOutcome.FAILED:
        failure = fatal_failure or GlobalWorkflowError(
            "summary",
            failures[-1]["category"] if failures else "NoArticlesPublished",
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
    elif outcome is RunOutcome.COST_GUARD_STOPPED:
        _send_alert(
            alert_publisher,
            _alert_lines(
                "⚠️ Daily Chip News 成本保护停止",
                outcome,
                stats,
                failures,
                breaker_opened=breaker_opened,
                fatal_failure=fatal_failure,
                status_code=None,
                cost_guard_reason=guard.state.reason,
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
    return _final_result(stats, outcome)


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
    ).publish_alert
    result = run_daily(settings, alert_publisher=alert_publisher)
    raise SystemExit(int(result["exit_code"]))
