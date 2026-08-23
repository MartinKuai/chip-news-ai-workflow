"""Writer: fresh, deliberately constrained context in; Chinese draft out."""

from __future__ import annotations

from typing import Any

from ..config import EDITORIAL_BRIEF
from ..schemas import GraphState, validate_draft
from .researcher import JSONClient


WRITER_OUTPUT_SCHEMA = {
    "headline": "concise Chinese headline",
    "summary": "80-160 Chinese characters",
    "key_facts": ["2-4 facts supported by Research Notes"],
    "why_it_matters": "1-2 concise sentences",
    "telegram_copy": "publishable Chinese copy within 500 Chinese characters",
}

WRITER_INSTRUCTION = """
你是 Writer（写手）。每次调用都是全新的干净上下文。
只能使用 payload 中的 Editorial Brief、Structured Research Notes、Revision Brief 和 Output Schema。
不得补充 Research Notes 中不存在的事实、数字、因果、预测或背景知识。
写作使用中文，简洁、克制、事实优先，不写营销腔。
返工时只执行精简 revision_brief，不接收或推断 Reviewer 的其他上下文。
严格返回符合 output_schema 的 JSON 对象。
""".strip()


class WriterNode:
    def __init__(self, client: JSONClient, model: str) -> None:
        self._client = client
        self._model = model

    def __call__(self, state: GraphState) -> dict[str, Any]:
        # Construct a new allow-listed context on every invocation.
        clean_payload = {
            "editorial_brief": dict(EDITORIAL_BRIEF),
            "research_notes": state["research_notes"],
            "revision_brief": list(state.get("revision_brief", [])),
            "output_schema": WRITER_OUTPUT_SCHEMA,
        }
        result = self._client.request_json(
            model=self._model,
            system_instruction=WRITER_INSTRUCTION,
            payload=clean_payload,
        )
        return {"draft": validate_draft(result), "status": "DRAFTED"}
