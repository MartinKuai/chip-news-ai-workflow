"""Writer: structured research notes in, Chinese draft out."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import EDITORIAL_BRIEF
from ..errors import record_health_outcome
from ..gemini import JSONClient
from ..health import HealthEvent
from ..metrics import RunMetrics
from ..schemas import GraphState, validate_draft


WRITER_OUTPUT_SCHEMA = {
    "headline": "concise Chinese headline",
    "summary": "80-160 Chinese characters",
    "key_facts": ["2-4 facts supported by Research Notes"],
    "why_it_matters": "1-2 concise sentences",
    "telegram_copy": "publishable Chinese copy within 500 Chinese characters",
}


WRITER_OUTPUT_SPEC = {
    "type": "OBJECT",
    "properties": {
        "headline": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "key_facts": {"type": "ARRAY", "items": {"type": "STRING"}},
        "why_it_matters": {"type": "STRING"},
        "telegram_copy": {"type": "STRING"},
    },
    "required": [
        "headline",
        "summary",
        "key_facts",
        "why_it_matters",
        "telegram_copy",
    ],
}


WRITER_INSTRUCTION = """
你是 Writer（写手）。每次调用都是全新的干净上下文。
只能使用 payload 中的 Editorial Brief、Structured Research Notes、Revision Brief 和 Output Schema。
不得补充 Research Notes 中不存在的事实、数字、因果、预测或背景知识。
写作使用中文，简洁、克制、事实优先，不写营销腔。
返工时只执行精简 revision_brief，只修改 previous_draft，不新增事实。
严格返回符合 output_schema 的 JSON 对象。
""".strip()


class WriterNode:
    """Compose from Research Notes; revise from the previous draft only."""

    def __init__(
        self,
        client: JSONClient,
        model: str,
        *,
        metrics: RunMetrics | None = None,
        health_recorder: Callable[[HealthEvent], None] | None = None,
        thinking_level: str = "low",
        max_output_tokens: int = 2560,
    ) -> None:
        self._client = client
        self._model = model
        self._metrics = metrics
        self._health_recorder = health_recorder
        self._thinking_level = thinking_level
        self._max_output_tokens = max_output_tokens

    def __call__(self, state: GraphState) -> dict[str, Any]:
        try:
            update = self._run(state)
        except Exception as exc:
            record_health_outcome(self._health_recorder, stage="writer", cause=exc)
            raise
        record_health_outcome(self._health_recorder, stage="writer")
        return update

    def _run(self, state: GraphState) -> dict[str, Any]:
        revision_brief = [
            str(item).strip()
            for item in state.get("revision_brief", [])
            if str(item).strip()
        ]
        previous_draft = state.get("draft") or {}
        payload: dict[str, Any] = {
            "editorial_brief": dict(EDITORIAL_BRIEF),
            "research_notes": state["research_notes"],
            "revision_brief": revision_brief,
            "output_schema": WRITER_OUTPUT_SCHEMA,
        }
        if previous_draft and revision_brief:
            payload["previous_draft"] = previous_draft
        result = self._client.request_json(
            model=self._model,
            system_instruction=WRITER_INSTRUCTION,
            payload=payload,
            purpose="writer",
            output_schema=WRITER_OUTPUT_SPEC,
            thinking_level=self._thinking_level,
            max_output_tokens=self._max_output_tokens,
        )
        return {"draft": validate_draft(result), "status": "DRAFTED"}
