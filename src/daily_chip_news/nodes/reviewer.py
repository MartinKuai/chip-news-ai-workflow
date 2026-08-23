"""Reviewer: machine-readable QA decision, never rewritten copy."""

from __future__ import annotations

from typing import Any

from ..config import REVIEW_RUBRIC
from ..schemas import GraphState, validate_review
from .researcher import JSONClient


REVIEW_OUTPUT_SCHEMA = {
    "status": "PASS or REJECT",
    "scores": {"factuality": "0-10", "relevance": "0-10", "clarity": "0-10"},
    "issues": [{"severity": "major or minor", "problem": "specific problem"}],
    "revision_brief": ["concise correction request; empty for PASS"],
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
    def __init__(self, client: JSONClient, model: str) -> None:
        self._client = client
        self._model = model

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
        )
        return {"review": validate_review(result)}
