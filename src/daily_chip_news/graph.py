"""The actual LangGraph StateGraph for the bounded editorial workflow."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from .config import Settings
from .gemini import GeminiClient
from .nodes import ResearcherNode, ReviewerNode, WriterNode
from .publisher import PublisherNode, TelegramPublisher
from .schemas import GraphState, SchemaError
from .sources import ArticleExtractor


GraphNode = Callable[[GraphState], dict[str, Any]]


def build_editorial_graph(
    *,
    researcher: GraphNode,
    writer: GraphNode,
    reviewer: GraphNode,
    publisher: GraphNode,
    max_revisions: int,
):
    """Build START -> Researcher -> Writer -> Reviewer with bounded routing."""
    if max_revisions < 0:
        raise ValueError("max_revisions cannot be negative")

    def research_node(state: GraphState) -> dict[str, Any]:
        update = researcher(state)
        notes = update.get("research_notes")
        if not isinstance(notes, dict):
            raise SchemaError("Researcher node did not return research_notes")
        decision = str(notes.get("decision", "")).upper()
        if decision not in {"KEEP", "SKIP"}:
            raise SchemaError("Researcher decision must be KEEP or SKIP")
        return {**update, "status": "SKIP" if decision == "SKIP" else "RESEARCHED"}

    def writer_node(state: GraphState) -> dict[str, Any]:
        update = writer(state)
        if not isinstance(update.get("draft"), dict):
            raise SchemaError("Writer node did not return a draft")
        return {**update, "status": "DRAFTED"}

    def review_node(state: GraphState) -> dict[str, Any]:
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

    def publish_node(state: GraphState) -> dict[str, Any]:
        update = publisher(state)
        return {**update, "published": True, "status": "PASS"}

    def after_researcher(state: GraphState) -> Literal["writer", "end"]:
        return "end" if state["status"] == "SKIP" else "writer"

    def after_reviewer(state: GraphState) -> Literal["writer", "publisher", "end"]:
        if state["status"] == "PASS":
            return "publisher"
        if state["status"] == "REJECT":
            return "writer"
        if state["status"] == "HOLD":
            return "end"
        raise SchemaError(f"Unexpected graph status after review: {state['status']}")

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


def create_runtime_graph(settings: Settings):
    """Wire real infrastructure while preserving independent model selection."""
    client = GeminiClient(settings.gemini_api_key)
    return build_editorial_graph(
        researcher=ResearcherNode(
            client, settings.researcher_model, ArticleExtractor()
        ),
        writer=WriterNode(client, settings.writer_model),
        reviewer=ReviewerNode(client, settings.reviewer_model),
        publisher=PublisherNode(
            TelegramPublisher(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
            )
        ),
        max_revisions=settings.max_revisions,
    )
