"""Data contracts shared by the pipeline stages and the graph nodes."""

from __future__ import annotations

from typing import Any, TypedDict


class SchemaError(RuntimeError):
    """Raised when an AI node violates its structured output contract."""


class Candidate(TypedDict, total=False):
    """One news candidate produced by source selection."""

    id: str
    title: str
    url: str
    source: str
    published_at: str
    metadata: dict[str, Any]


class ResearchNote(TypedDict):
    claim: str
    evidence: str
    why_it_matters: str
    confidence: float


class Entities(TypedDict):
    companies: list[str]
    products: list[str]
    models: list[str]
    events: list[str]


class KeyNumber(TypedDict):
    label: str
    value: str


class ResearchNotes(TypedDict):
    decision: str
    reason: str
    topic: str
    source: str
    url: str
    published_at: str
    entities: Entities
    key_numbers: list[KeyNumber]
    gaps: list[str]
    notes: list[ResearchNote]


class Draft(TypedDict):
    headline: str
    summary: str
    key_facts: list[str]
    why_it_matters: str
    telegram_copy: str


class ReviewIssue(TypedDict):
    severity: str
    problem: str


class Review(TypedDict):
    status: str
    scores: dict[str, int]
    issues: list[ReviewIssue]
    revision_brief: list[str]


class GraphState(TypedDict, total=False):
    candidate: Candidate
    research_notes: ResearchNotes
    draft: Draft
    review: Review
    revision_brief: list[str]
    revision_count: int
    status: str
    published: bool


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be a JSON object")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise SchemaError(f"{label} must be a non-empty string")
    return value.strip()


def _text_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError(f"{label} must be a list")
    return [_text(item, f"{label} item") for item in value]


def _entities(value: Any) -> Entities:
    """Entities are a research aid, so a missing block degrades to empty lists."""
    if value is None:
        value = {}
    data = _mapping(value, "entities")
    return {
        "companies": _text_list(data.get("companies"), "entities.companies"),
        "products": _text_list(data.get("products"), "entities.products"),
        "models": _text_list(data.get("models"), "entities.models"),
        "events": _text_list(data.get("events"), "entities.events"),
    }


def _key_numbers(value: Any) -> list[KeyNumber]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError("key_numbers must be a list")
    numbers: list[KeyNumber] = []
    for index, item in enumerate(value):
        entry = _mapping(item, f"key_numbers[{index}]")
        numbers.append(
            {
                "label": _text(entry.get("label"), f"key_numbers[{index}].label"),
                "value": _text(entry.get("value"), f"key_numbers[{index}].value"),
            }
        )
    return numbers


def validate_research_notes(value: Any) -> ResearchNotes:
    data = _mapping(value, "Researcher output")
    decision = _text(data.get("decision"), "decision").upper()
    if decision not in {"KEEP", "SKIP"}:
        raise SchemaError("decision must be KEEP or SKIP")

    raw_notes = data.get("notes", [])
    if not isinstance(raw_notes, list):
        raise SchemaError("notes must be a list")
    notes: list[ResearchNote] = []
    if decision == "SKIP":
        # A SKIP decision is a routing signal; partial notes must not fail the item.
        raw_notes = []
    for index, item in enumerate(raw_notes):
        note = _mapping(item, f"notes[{index}]")
        try:
            confidence = float(note.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"notes[{index}].confidence must be numeric") from exc
        if not 0.0 <= confidence <= 1.0:
            raise SchemaError(f"notes[{index}].confidence must be between 0 and 1")
        notes.append(
            {
                "claim": _text(note.get("claim"), f"notes[{index}].claim"),
                "evidence": _text(note.get("evidence"), f"notes[{index}].evidence"),
                "why_it_matters": _text(
                    note.get("why_it_matters"), f"notes[{index}].why_it_matters"
                ),
                "confidence": confidence,
            }
        )
    if decision == "KEEP" and not notes:
        raise SchemaError("KEEP requires at least one research note")

    return {
        "decision": decision,
        "reason": _text(data.get("reason", ""), "reason", allow_empty=True),
        "topic": _text(data.get("topic", ""), "topic", allow_empty=decision == "SKIP"),
        "source": _text(data.get("source"), "source"),
        "url": _text(data.get("url"), "url"),
        "published_at": _text(
            data.get("published_at", ""), "published_at", allow_empty=True
        ),
        "entities": _entities(data.get("entities")),
        "key_numbers": _key_numbers(data.get("key_numbers")),
        "gaps": _text_list(data.get("gaps"), "gaps"),
        "notes": notes,
    }


def validate_draft(value: Any) -> Draft:
    data = _mapping(value, "Writer output")
    facts = data.get("key_facts")
    if not isinstance(facts, list) or not facts:
        raise SchemaError("key_facts must be a non-empty list")
    return {
        "headline": _text(data.get("headline"), "headline"),
        "summary": _text(data.get("summary"), "summary"),
        "key_facts": [_text(item, "key_facts item") for item in facts],
        "why_it_matters": _text(data.get("why_it_matters"), "why_it_matters"),
        "telegram_copy": _text(data.get("telegram_copy"), "telegram_copy"),
    }


def validate_review(value: Any) -> Review:
    data = _mapping(value, "Reviewer output")
    status = _text(data.get("status"), "status").upper()
    if status not in {"PASS", "REVISE", "REJECT"}:
        raise SchemaError("review status must be PASS, REVISE or REJECT")

    raw_scores = _mapping(data.get("scores"), "scores")
    scores: dict[str, int] = {}
    for name in ("factuality", "relevance", "clarity"):
        raw_score = raw_scores.get(name)
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise SchemaError(f"scores.{name} must be numeric")
        score = int(raw_score)
        if not 0 <= score <= 10:
            raise SchemaError(f"scores.{name} must be between 0 and 10")
        scores[name] = score

    raw_issues = data.get("issues", [])
    if not isinstance(raw_issues, list):
        raise SchemaError("issues must be a list")
    issues: list[ReviewIssue] = []
    for index, item in enumerate(raw_issues):
        issue = _mapping(item, f"issues[{index}]")
        severity = _text(issue.get("severity"), f"issues[{index}].severity").lower()
        if severity not in {"major", "minor"}:
            raise SchemaError("issue severity must be major or minor")
        issues.append(
            {
                "severity": severity,
                "problem": _text(issue.get("problem"), f"issues[{index}].problem"),
            }
        )

    brief = _text_list(data.get("revision_brief"), "revision_brief")
    if status == "PASS" and brief:
        raise SchemaError("PASS must have an empty revision_brief")
    if status == "REVISE" and not brief:
        raise SchemaError("REVISE requires a concise revision_brief")
    if status == "REJECT" and not issues:
        raise SchemaError("REJECT requires at least one issue")
    if status == "PASS" and (
        scores["factuality"] < 8
        or scores["relevance"] < 8
        or any(issue["severity"] == "major" for issue in issues)
    ):
        raise SchemaError("Reviewer PASS violates the deterministic QA threshold")
    return {
        "status": status,
        "scores": scores,
        "issues": issues,
        "revision_brief": brief,
    }
