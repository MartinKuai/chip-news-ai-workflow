"""Small data contracts shared by the graph and its nodes."""

from __future__ import annotations

from typing import Any, TypedDict


class SchemaError(RuntimeError):
    """Raised when an AI node violates its structured output contract."""


class Article(TypedDict):
    title: str
    source: str
    url: str
    published_at: str


class ResearchNote(TypedDict):
    claim: str
    evidence: str
    why_it_matters: str
    confidence: float


class ResearchNotes(TypedDict):
    decision: str
    reason: str
    topic: str
    source: str
    url: str
    published_at: str
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
    article: Article
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


def validate_research_notes(value: Any) -> ResearchNotes:
    data = _mapping(value, "Researcher output")
    decision = _text(data.get("decision"), "decision").upper()
    if decision not in {"KEEP", "SKIP"}:
        raise SchemaError("decision must be KEEP or SKIP")

    raw_notes = data.get("notes", [])
    if not isinstance(raw_notes, list):
        raise SchemaError("notes must be a list")
    notes: list[ResearchNote] = []
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
    if status not in {"PASS", "REJECT"}:
        raise SchemaError("review status must be PASS or REJECT")

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

    raw_brief = data.get("revision_brief", [])
    if not isinstance(raw_brief, list):
        raise SchemaError("revision_brief must be a list")
    brief = [_text(item, "revision_brief item") for item in raw_brief]
    if status == "PASS" and brief:
        raise SchemaError("PASS must have an empty revision_brief")
    if status == "REJECT" and not brief:
        raise SchemaError("REJECT requires a concise revision_brief")
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
