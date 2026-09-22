"""Reviewer: machine-readable QA decision, never rewritten copy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import REVIEW_RUBRIC, NodeProfile
from ..errors import record_health_outcome
from ..gemini import JSONClient
from ..health import HealthEvent
from ..metrics import RunMetrics
from ..schemas import GraphState, validate_review

REVIEW_OUTPUT_SCHEMA = {
    "status": "PASS or REVISE or REJECT",
    "scores": {"factuality": "0-10", "relevance": "0-10", "clarity": "0-10"},
    "issues": [{"severity": "major or minor", "problem": "specific problem"}],
    "revision_brief": ["concise correction request; empty for PASS"],
}


REVIEW_OUTPUT_SPEC = {
    "type": "OBJECT",
    "properties": {
        "status": {"type": "STRING", "enum": ["PASS", "REVISE", "REJECT"]},
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
逐项使用 rubric 对照 Research Notes 检查 Draft，重点检查：
事实一致性、公司名 / 产品名 / 芯片型号 / 数字的准确性、是否存在无依据扩写、
文章结构，以及是否达到可发布质量。
任何无 Research Notes 支持的事实、数字、因果或预测都是 major issue。
factuality 或 relevance 低于 8，或存在 major issue，必须 REVISE 或 REJECT。
status 取值：
- PASS：达到发布质量；issues 可为空且 revision_brief 必须为空。
- REVISE：可以通过修改 draft 修好；必须给出精简、明确的 revision_brief 修改指令。
- REJECT：无法通过改写修复（例如整篇缺乏原文依据）；必须给出具体 issues。
不提供重写后的正文。严格返回符合 output_schema 的 JSON 对象。
""".strip()


class ReviewerNode:
    def __init__(
        self,
        client: JSONClient,
        profile: NodeProfile,
        *,
        metrics: RunMetrics | None = None,
        health_recorder: Callable[[HealthEvent], None] | None = None,
    ) -> None:
        self._client = client
        self._profile = profile
        self._metrics = metrics
        self._health_recorder = health_recorder

    def __call__(self, state: GraphState) -> dict[str, Any]:
        try:
            result = self._client.request_json(
                model=self._profile.model,
                system_instruction=REVIEWER_INSTRUCTION,
                payload={
                    "research_notes": state["research_notes"],
                    "draft": state["draft"],
                    "rubric": list(REVIEW_RUBRIC),
                    "output_schema": REVIEW_OUTPUT_SCHEMA,
                },
                purpose="reviewer",
                output_schema=REVIEW_OUTPUT_SPEC,
                thinking_level=self._profile.thinking_level,
                max_output_tokens=self._profile.max_output_tokens,
            )
            review = validate_review(result)
        except Exception as exc:
            record_health_outcome(self._health_recorder, stage="reviewer", cause=exc)
            raise
        record_health_outcome(self._health_recorder, stage="reviewer")
        return {"review": review}
