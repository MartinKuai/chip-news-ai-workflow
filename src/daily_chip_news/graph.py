"""The editorial pipeline graph: Researcher -> Writer -> Reviewer loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from .config import Settings
from .gemini import GeminiClient
from .health import ServiceHealth
from .metrics import RunMetrics
from .nodes import ResearcherNode, ReviewerNode, WriterNode
from .publisher import PublisherNode, TelegramPublisher
from .schemas import GraphState, SchemaError
from .sources import ArticleExtractor

GraphNode = Callable[[GraphState], dict[str, Any]]
StepRecorder = Callable[[str, str], None]

STATUS_NEW = "NEW"
STATUS_SKIP = "SKIP"
STATUS_RESEARCHED = "RESEARCHED"
STATUS_DRAFTED = "DRAFTED"
STATUS_PASS = "PASS"
STATUS_REVISE = "REVISE"
STATUS_REJECT = "REJECT"
STATUS_HOLD = "HOLD"
STATUS_PUBLISHED = "PUBLISHED"


class NodeExecutionError(RuntimeError):
    """Attach a safe graph stage to an underlying node failure."""

    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage} failed with {type(cause).__name__}")


def build_editorial_graph(
    *,
    researcher: GraphNode,
    writer: GraphNode,
    reviewer: GraphNode,
    publisher: GraphNode,
    max_revisions: int,
    on_step: StepRecorder | None = None,
):
    """Build START -> Researcher -> Writer -> Reviewer with bounded routing.

    Terminal statuses: ``SKIP`` (not relevant), ``REJECT`` (not publishable),
    ``HOLD`` (revision limit reached) and ``PUBLISHED``.
    """
    if max_revisions < 0:
        raise ValueError("max_revisions cannot be negative")

    def record(stage: str, status: str) -> None:
        if on_step is not None:
            on_step(stage, status)

    def research_node(state: GraphState) -> dict[str, Any]:
        try:
            update = researcher(state)
            notes = update.get("research_notes")
            if not isinstance(notes, dict):
                raise SchemaError("Researcher node did not return research_notes")
            decision = str(notes.get("decision", "")).upper()
            if decision not in {"KEEP", "SKIP"}:
                raise SchemaError("Researcher decision must be KEEP or SKIP")
            status = STATUS_SKIP if decision == "SKIP" else STATUS_RESEARCHED
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("researcher", exc) from exc
        record("researcher", status)
        return {**update, "status": status}

    def writer_node(state: GraphState) -> dict[str, Any]:
        try:
            update = writer(state)
            draft = update.get("draft")
            if not isinstance(draft, dict) or not draft:
                raise SchemaError("Writer node did not return a draft")
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("writer", exc) from exc
        record("writer", STATUS_DRAFTED)
        return {**update, "status": STATUS_DRAFTED}

    def review_node(state: GraphState) -> dict[str, Any]:
        try:
            update = reviewer(state)
            review = update.get("review")
            if not isinstance(review, dict):
                raise SchemaError("Reviewer node did not return a review")
            decision = str(review.get("status", "")).upper()
            brief = review.get("revision_brief", [])
            if decision == STATUS_PASS:
                result = {**update, "status": STATUS_PASS, "revision_brief": []}
            elif decision == STATUS_REJECT:
                result = {
                    **update,
                    "status": STATUS_REJECT,
                    "revision_brief": list(brief) if isinstance(brief, list) else [],
                }
            elif decision == STATUS_REVISE:
                if not isinstance(brief, list) or not brief:
                    raise SchemaError("Reviewer REVISE requires revision_brief")
                revision_count = int(state.get("revision_count", 0))
                if revision_count >= max_revisions:
                    # Hard revision limit: keep the instructions for the summary.
                    result = {
                        **update,
                        "status": STATUS_HOLD,
                        "revision_brief": list(brief),
                        "revision_count": revision_count,
                    }
                else:
                    result = {
                        **update,
                        "status": STATUS_REVISE,
                        "revision_brief": list(brief),
                        "revision_count": revision_count + 1,
                    }
            else:
                raise SchemaError("Reviewer status must be PASS, REVISE or REJECT")
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("reviewer", exc) from exc
        record("reviewer", result["status"])
        return result

    def publish_node(state: GraphState) -> dict[str, Any]:
        try:
            update = publisher(state)
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("publisher", exc) from exc
        record("publisher", STATUS_PUBLISHED)
        return {**update, "published": True, "status": STATUS_PUBLISHED}

    def after_researcher(state: GraphState) -> Literal["writer", "end"]:
        return "end" if state["status"] == STATUS_SKIP else "writer"

    def after_reviewer(state: GraphState) -> Literal["writer", "publisher", "end"]:
        status = state["status"]
        if status == STATUS_PASS:
            return "publisher"
        if status == STATUS_REVISE:
            return "writer"
        if status in {STATUS_REJECT, STATUS_HOLD}:
            return "end"
        raise SchemaError(f"Unexpected graph status after review: {status}")

    builder = StateGraph(GraphState)
    builder.add_node("researcher", research_node)
    builder.add_node("writer", writer_node)
    builder.add_node("reviewer", review_node)
    builder.add_node("publisher", publish_node)
    builder.add_edge(START, "researcher")
    builder.add_conditional_edges(
        "researcher", after_researcher, {"writer": "writer", "end": END}
    )
    builder.add_edge("writer", "reviewer")
    builder.add_conditional_edges(
        "reviewer",
        after_reviewer,
        {"writer": "writer", "publisher": "publisher", "end": END},
    )
    builder.add_edge("publisher", END)
    return builder.compile()


def create_runtime_graph(
    settings: Settings,
    *,
    metrics: RunMetrics | None = None,
    breaker: ServiceHealth | None = None,
    run_deadline: float | None = None,
    on_step: StepRecorder | None = None,
):
    """Wire real infrastructure while preserving independent model selection."""
    run_metrics = metrics or RunMetrics()
    record = breaker.record if breaker is not None else None
    client = GeminiClient(
        settings.gemini_api_key,
        timeout=settings.gemini_timeout_seconds,
        max_attempts=settings.gemini_max_attempts,
        call_budget_seconds=settings.gemini_call_budget_seconds,
        structured_output=settings.gemini_structured_output,
        run_deadline=run_deadline,
        logger=print,
        metrics=run_metrics,
    )
    return build_editorial_graph(
        researcher=ResearcherNode(
            client,
            settings.researcher,
            ArticleExtractor(),
            metrics=run_metrics,
            health_recorder=record,
            max_content_chars=settings.article_content_chars,
        ),
        writer=WriterNode(
            client,
            settings.writer,
            metrics=run_metrics,
            health_recorder=record,
        ),
        reviewer=ReviewerNode(
            client,
            settings.reviewer,
            metrics=run_metrics,
            health_recorder=record,
        ),
        publisher=PublisherNode(
            TelegramPublisher(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                timeout=settings.telegram_timeout_seconds,
                enabled=settings.publish_enabled,
            )
        ),
        max_revisions=settings.max_revisions,
        on_step=on_step,
    )
