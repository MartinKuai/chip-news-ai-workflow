"""The actual LangGraph StateGraph for the bounded editorial workflow."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from .config import Settings
from .gemini import GeminiClient
from .health import ServiceHealth
from .metrics import RunMetrics
from .nodes import ReviewerNode, WriterNode
from .publisher import PublisherNode, TelegramPublisher
from .schemas import GraphState, SchemaError
from .sources import ArticleExtractor


GraphNode = Callable[[GraphState], dict[str, Any]]


class NodeExecutionError(RuntimeError):
    """Attach a safe graph stage to an underlying node failure."""

    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage} failed with {type(cause).__name__}")


def build_editorial_graph(
    *,
    writer: GraphNode,
    reviewer: GraphNode,
    publisher: GraphNode,
    max_revisions: int,
):
    """Build START -> Writer -> Reviewer with bounded routing."""
    if max_revisions < 0:
        raise ValueError("max_revisions cannot be negative")

    def writer_node(state: GraphState) -> dict[str, Any]:
        try:
            update = writer(state)
            notes = update.get("research_notes")
            if not isinstance(notes, dict):
                raise SchemaError("Writer node did not return research_notes")
            decision = str(notes.get("decision", "")).upper()
            if decision not in {"KEEP", "SKIP"}:
                raise SchemaError("Writer decision must be KEEP or SKIP")
            draft = update.get("draft", {})
            if not isinstance(draft, dict):
                raise SchemaError("Writer node did not return a draft object")
            if decision == "KEEP" and not draft:
                raise SchemaError("Writer KEEP requires a non-empty draft")
            return {
                **update,
                "status": "SKIP" if decision == "SKIP" else "DRAFTED",
            }
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("writer", exc) from exc

    def review_node(state: GraphState) -> dict[str, Any]:
        try:
            update = reviewer(state)
            review = update.get("review")
            if not isinstance(review, dict):
                raise SchemaError("Reviewer node did not return a review")
            decision = str(review.get("status", "")).upper()
            if decision == "PASS":
                return {
                    **update,
                    "status": "PASS",
                    "revision_brief": [],
                }
            if decision != "REJECT":
                raise SchemaError("Reviewer status must be PASS or REJECT")

            brief = review.get("revision_brief", [])
            if not isinstance(brief, list) or not brief:
                raise SchemaError("Reviewer REJECT requires revision_brief")
            revision_count = int(state.get("revision_count", 0))
            if revision_count >= max_revisions:
                return {
                    **update,
                    "status": "HOLD",
                    "revision_brief": list(brief),
                    "revision_count": revision_count,
                }
            return {
                **update,
                "status": "REJECT",
                "revision_brief": list(brief),
                "revision_count": revision_count + 1,
            }
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("reviewer", exc) from exc

    def publish_node(state: GraphState) -> dict[str, Any]:
        try:
            update = publisher(state)
            return {**update, "published": True, "status": "PASS"}
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("publisher", exc) from exc

    def after_writer(state: GraphState) -> Literal["reviewer", "end"]:
        return "end" if state["status"] == "SKIP" else "reviewer"

    def after_reviewer(state: GraphState) -> Literal["writer", "publisher", "end"]:
        if state["status"] == "PASS":
            return "publisher"
        if state["status"] == "REJECT":
            return "writer"
        if state["status"] == "HOLD":
            return "end"
        raise SchemaError(f"Unexpected graph status after review: {state['status']}")

    builder = StateGraph(GraphState)
    builder.add_node("writer", writer_node)
    builder.add_node("reviewer", review_node)
    builder.add_node("publisher", publish_node)
    builder.add_edge(START, "writer")
    builder.add_conditional_edges(
        "writer", after_writer, {"reviewer": "reviewer", "end": END}
    )
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
    health: ServiceHealth | None = None,
    run_deadline: float | None = None,
):
    """Wire real infrastructure while preserving independent model selection."""
    run_metrics = metrics or RunMetrics()
    health_recorder = health.record if health is not None else None
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
        writer=WriterNode(
            client,
            settings.writer_model,
            ArticleExtractor(),
            metrics=run_metrics,
            health_recorder=health_recorder,
            thinking_level=settings.writer_thinking_level,
            max_output_tokens=settings.writer_max_output_tokens,
            max_content_chars=settings.article_content_chars,
        ),
        reviewer=ReviewerNode(
            client,
            settings.reviewer_model,
            metrics=run_metrics,
            health_recorder=health_recorder,
            thinking_level=settings.reviewer_thinking_level,
            max_output_tokens=settings.reviewer_max_output_tokens,
        ),
        publisher=PublisherNode(
            TelegramPublisher(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
            )
        ),
        max_revisions=settings.max_revisions,
    )
