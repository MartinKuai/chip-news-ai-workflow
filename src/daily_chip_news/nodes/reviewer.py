"""Reviewer: machine-readable QA decision, never rewritten copy."""

from __future__ import annotations

from typing import Any

from ..config import REVIEW_RUBRIC
from ..gemini import JSONClient
from ..metrics import RunMetrics
from ..schemas import GraphState, validate_review


REVIEW_OUTPUT_SCHEMA = {
    "status": "PASS or REJECT",
    "scores": {"factuality": "0-10", "relevance": "0-10", "clarity": "0-10"},
    "issues": [{"severity": "major or minor", "problem": "specific problem"}],
    "revision_brief": ["concise correction request; empty for PASS"],
}


REVIEW_OUTPUT_SPEC = {
    "type": "OBJECT",
    "properties": {
        "status": {"type": "STRING", "enum": ["PASS", "REJECT"]},
        "scores": {
            "type": "OBJECT",
            "properties": {
                "factuality": {"type": "INTEGER"},
                "relevance": {"type": "INTEGER"},
                "clarity": {"type": "INTEGER"},
            },
            "required": ["factuality", "relevance", "clarity"],
        },
        "issues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "severity": {"type": "STRING", "enum": ["major", "minor"]},
                    "problem": {"type": "STRING"},
                },
                "required": ["severity", "problem"],
            },
        },
        "revision_brief": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["status", "scores", "issues", "revision_brief"],
}


REVIEWER_INSTRUCTION = """
你是 Reviewer（审稿人），只审核，不重写文章。
逐项使用 rubric 对照 Research Notes 检查 Draft。
任何无 Research Notes 支持的事实、数字、因果或预测都是 major issue。
factuality 或 relevance 低于 8，或存在 major issue，必须 REJECT。
PASS 时 issues 可为空且 revision_brief 必须为空。
REJECT 时给出具体问题和精简 revision_brief，不提供重写后的正文。
严格返回符合 output_schema 的 JSON 对象。
""".strip()


class ReviewerNode:
    def __init__(
        self,
        client: JSONClient,
        model: str,
        *,
        metrics: RunMetrics | None = None,
        thinking_level: str = "low",
        max_output_tokens: int = 8192,
    ) -> None:
        self._client = client
        self._model = model
        self._metrics = metrics
        self._thinking_level = thinking_level
        self._max_output_tokens = max_output_tokens

    def __call__(self, state: GraphState) -> dict[str, Any]:
        result = self._client.request_json(
            model=self._model,
            system_instruction=REVIEWER_INSTRUCTION,
            payload={
                "research_notes": state["research_notes"],
                "draft": state["draft"],
                "rubric": list(REVIEW_RUBRIC),
                "output_schema": REVIEW_OUTPUT_SCHEMA,
            },
            purpose="reviewer",
            output_schema=REVIEW_OUTPUT_SPEC,
            thinking_level=self._thinking_level,
            max_output_tokens=self._max_output_tokens,
        )
        return {"review": validate_review(result)}
