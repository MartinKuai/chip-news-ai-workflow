"""Writer: one high-quality call that researches the source and writes the draft."""

from __future__ import annotations

from typing import Any

from ..config import EDITORIAL_BRIEF, EDITORIAL_SCOPE
from ..gemini import JSONClient
from ..metrics import RunMetrics
from ..schemas import GraphState, validate_writer_output
from ..sources import SourceError, clean_extracted_text


WRITER_OUTPUT_SCHEMA = {
    "decision": "KEEP or SKIP",
    "reason": "short reason",
    "topic": "topic or empty for SKIP",
    "notes": [
        {
            "claim": "source-supported fact",
            "evidence": "concise evidence summary",
            "why_it_matters": "commercial or technical relevance",
            "confidence": "number from 0 to 1",
        }
    ],
    "draft": {
        "headline": "concise Chinese headline",
        "summary": "80-160 Chinese characters",
        "key_facts": ["2-4 facts supported by the source"],
        "why_it_matters": "1-2 concise sentences",
        "telegram_copy": "publishable Chinese copy within 500 Chinese characters",
    },
}


WRITER_OUTPUT_SPEC = {
    "type": "OBJECT",
    "properties": {
        "decision": {"type": "STRING", "enum": ["KEEP", "SKIP"]},
        "reason": {"type": "STRING"},
        "topic": {"type": "STRING"},
        "notes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "claim": {"type": "STRING"},
                    "evidence": {"type": "STRING"},
                    "why_it_matters": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": [
                    "claim",
                    "evidence",
                    "why_it_matters",
                    "confidence",
                ],
            },
        },
        "draft": {
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
        },
    },
    "required": ["decision", "reason", "topic", "notes", "draft"],
}


WRITER_INSTRUCTION = """
你是 Writer（写手）。一次调用同时完成"判断相关性 + 提取事实 + 中文成稿"，只输出结构化 JSON。

payload 的 mode 有两种：
- compose：article_metadata 与 raw_content 是唯一事实来源。先按 editorial_scope 判断相关性；
  无关、信息量不足、纯列表/广告/招聘时输出 decision=SKIP，reason 说明原因，notes 为空，draft 为空对象。
- revise：research_notes 是唯一事实来源，previous_draft 是上一版草稿，revision_brief 是审稿人要求。
  只按 revision_brief 修改 previous_draft，不重新取材，不新增事实。

硬性要求：
1. 不得补充事实来源中不存在的数字、时间、人物、因果、预测或背景知识。
2. decision=KEEP 时输出 1-4 条 notes：claim、evidence（事实来源可支持的依据）、
   why_it_matters、confidence（0 到 1）。
3. draft 使用中文，简洁、克制、事实优先，不写营销腔：headline、summary（80-160 字）、
   key_facts（2-4 条）、why_it_matters（1-2 句）、telegram_copy（不超过 500 字）。
4. 严格返回符合 output_schema 的 JSON 对象，不要 Markdown 代码块，不要解释。
""".strip()


class WriterNode:
    """Compose mode researches from cleaned source text; revise mode only edits."""

    def __init__(
        self,
        client: JSONClient,
        model: str,
        extractor,
        *,
        metrics: RunMetrics | None = None,
        thinking_level: str = "medium",
        max_output_tokens: int = 16384,
        max_content_chars: int = 16000,
    ) -> None:
        self._client = client
        self._model = model
        self._extractor = extractor
        self._metrics = metrics
        self._thinking_level = thinking_level
        self._max_output_tokens = max_output_tokens
        self._max_content_chars = max_content_chars

    def __call__(self, state: GraphState) -> dict[str, Any]:
        article = state["article"]
        revision_brief = [
            str(item).strip()
            for item in state.get("revision_brief", [])
            if str(item).strip()
        ]
        previous_draft = state.get("draft") or {}
        if previous_draft and revision_brief:
            payload = {
                "mode": "revise",
                "article_metadata": dict(article),
                "research_notes": state["research_notes"],
                "previous_draft": previous_draft,
                "revision_brief": revision_brief,
                "editorial_brief": dict(EDITORIAL_BRIEF),
                "output_schema": WRITER_OUTPUT_SCHEMA,
            }
        else:
            raw_content = self._extractor(article["url"])
            content = clean_extracted_text(
                raw_content, max_chars=self._max_content_chars
            )
            if not content.strip():
                raise SourceError("Article extraction returned no readable content")
            payload = {
                "mode": "compose",
                "article_metadata": dict(article),
                "raw_content": content,
                "editorial_scope": list(EDITORIAL_SCOPE),
                "editorial_brief": dict(EDITORIAL_BRIEF),
                "output_schema": WRITER_OUTPUT_SCHEMA,
            }

        result = self._client.request_json(
            model=self._model,
            system_instruction=WRITER_INSTRUCTION,
            payload=payload,
            purpose="writer",
            output_schema=WRITER_OUTPUT_SPEC,
            thinking_level=self._thinking_level,
            max_output_tokens=self._max_output_tokens,
        )
        notes, draft = validate_writer_output(
            result,
            source=article["source"],
            url=article["url"],
            published_at=article.get("published_at", ""),
        )
        if payload["mode"] == "revise":
            # A revision must not change the factual basis: keep the original notes
            # deterministically so the Reviewer always checks against them.
            notes = state["research_notes"]
        if draft is None:
            return {
                "research_notes": notes,
                "draft": {},
                "status": "SKIP",
            }
        return {
            "research_notes": notes,
            "draft": draft,
            "status": "DRAFTED",
        }
