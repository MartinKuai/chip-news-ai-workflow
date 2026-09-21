from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


ARTICLE = {
    "title": "HBM update",
    "source": "Example Source",
    "url": "https://example.com/hbm",
    "published_at": "2026-08-24",
}

NOTES = [
    {
        "claim": "A vendor announced a capacity update.",
        "evidence": "The source directly describes the capacity update.",
        "why_it_matters": "It may affect supplier discussions.",
        "confidence": 0.9,
    }
]

RESEARCH = {
    "decision": "KEEP",
    "reason": "Relevant supply-chain update",
    "topic": "HBM supply",
    "source": "Example Source",
    "url": "https://example.com/hbm",
    "published_at": "2026-08-24",
    "notes": NOTES,
}

DRAFT = {
    "headline": "HBM 供应动态",
    "summary": "一家供应商公布了产能更新，相关信息可用于后续供应沟通。",
    "key_facts": ["供应商公布产能更新"],
    "why_it_matters": "该动态有助于理解后续供应沟通重点。",
    "telegram_copy": "HBM 供应动态：一家供应商公布了产能更新。",
}

WRITER_OUTPUT = {
    "decision": "KEEP",
    "reason": "Relevant supply-chain update",
    "topic": "HBM supply",
    "notes": copy.deepcopy(NOTES),
    "draft": dict(DRAFT),
}

SKIP_OUTPUT = {
    "decision": "SKIP",
    "reason": "Not in editorial scope",
    "topic": "",
    "notes": [],
    "draft": {},
}


def writer_output(**overrides: Any) -> dict[str, Any]:
    """Return a fresh valid merged Writer output, with optional overrides."""
    value = copy.deepcopy(WRITER_OUTPUT)
    value.update(overrides)
    return value


def skip_output(**overrides: Any) -> dict[str, Any]:
    value = copy.deepcopy(SKIP_OUTPUT)
    value.update(overrides)
    return value


def review(status: str) -> dict[str, Any]:
    rejected = status == "REJECT"
    return {
        "status": status,
        "scores": {"factuality": 9, "relevance": 9, "clarity": 9},
        "issues": (
            [{"severity": "major", "problem": "Remove unsupported wording"}]
            if rejected
            else []
        ),
        "revision_brief": ["删除缺乏依据的表述"] if rejected else [],
    }


class CaptureClient:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("No fake response configured")
        return self.responses.pop(0)
