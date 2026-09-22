from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from daily_chip_news.config import NodeProfile, Settings  # noqa: E402

CANDIDATE = {
    "id": "cand0001",
    "title": "HBM supplier raises capacity target",
    "url": "https://example.com/hbm",
    "source": "Example Source",
    "published_at": "2026-09-23T02:00:00+00:00",
    "metadata": {
        "feed_url": "https://example.com/feed",
        "feed_title": "Example Source",
    },
}

NOTES = [
    {
        "claim": "A vendor announced a capacity update.",
        "evidence": "The source directly describes the capacity update.",
        "why_it_matters": "It may affect supplier discussions.",
        "confidence": 0.9,
    }
]

ENTITIES = {
    "companies": ["Example Vendor"],
    "products": ["HBM3E"],
    "models": ["HBM3E 12-Hi"],
    "events": ["capacity target raised"],
}

RESEARCH = {
    "decision": "KEEP",
    "reason": "Relevant supply-chain update",
    "topic": "HBM supply",
    "source": "Example Source",
    "url": "https://example.com/hbm",
    "published_at": "2026-09-23T02:00:00+00:00",
    "entities": ENTITIES,
    "key_numbers": [{"label": "capacity increase", "value": "50%"}],
    "gaps": [],
    "notes": NOTES,
}

DRAFT = {
    "headline": "HBM 供应动态",
    "summary": "一家供应商公布了产能更新，相关信息可用于后续供应沟通。",
    "key_facts": ["供应商公布产能更新"],
    "why_it_matters": "该动态有助于理解后续供应沟通重点。",
    "telegram_copy": "HBM 供应动态：一家供应商公布了产能更新。",
}

PASS_REVIEW = {
    "status": "PASS",
    "scores": {"factuality": 9, "relevance": 9, "clarity": 9},
    "issues": [],
    "revision_brief": [],
}


def research_output(**overrides: Any) -> dict[str, Any]:
    """Researcher output without provenance (the node injects it)."""
    value = {
        "decision": "KEEP",
        "reason": "Relevant supply-chain update",
        "topic": "HBM supply",
        "entities": dict(ENTITIES),
        "key_numbers": [{"label": "capacity increase", "value": "50%"}],
        "gaps": [],
        "notes": [dict(note) for note in NOTES],
    }
    value.update(overrides)
    return value


def review(status: str, **overrides: Any) -> dict[str, Any]:
    value = {
        "status": status,
        "scores": {"factuality": 9, "relevance": 9, "clarity": 9},
        "issues": [],
        "revision_brief": [],
    }
    if status == "REVISE":
        value["issues"] = [{"severity": "major", "problem": "Unsupported wording"}]
        value["revision_brief"] = ["删除缺乏依据的表述"]
    elif status == "REJECT":
        value["issues"] = [{"severity": "major", "problem": "No source support"}]
    value.update(overrides)
    return value


def make_settings(**overrides: Any) -> Settings:
    """Minimal valid settings for runner and graph tests."""
    values: dict[str, Any] = {
        "gemini_api_key": "test-key",
        "researcher": NodeProfile(model="researcher-model", max_output_tokens=3072),
        "writer": NodeProfile(model="writer-model", max_output_tokens=2560),
        "reviewer": NodeProfile(model="reviewer-model", max_output_tokens=1024),
        "telegram_bot_token": "telegram-token",
        "telegram_chat_id": "telegram-chat",
        "publish_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class CaptureClient:
    """Scripted JSONClient: records every call and replays queued responses."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("No fake response configured")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else ""

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeSession:
    """requests.Session stand-in with queued responses or a routing function."""

    def __init__(
        self,
        *responses: Any,
        handler: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.handler = handler

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._respond(url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._respond(url, kwargs)

    def _respond(self, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        if self.handler is not None:
            response = self.handler(url, kwargs)
        else:
            if not self.responses:
                raise AssertionError(f"No fake response configured for {url}")
            response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def posts(self, fragment: str) -> list[dict[str, Any]]:
        return [
            kwargs
            for method, url, kwargs in self.calls
            if method == "POST" and fragment in url
        ]


def gemini_response(payload: Any, *, finish: str = "STOP") -> FakeResponse:
    """A successful Gemini ``generateContent`` envelope."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False)
    return FakeResponse(
        200,
        {
            "candidates": [
                {"content": {"parts": [{"text": text}]}, "finishReason": finish}
            ]
        },
    )
