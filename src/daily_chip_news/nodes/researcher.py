"""Researcher: raw source in, structured evidence notes out."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..config import EDITORIAL_SCOPE
from ..schemas import GraphState, validate_research_notes


class JSONClient(Protocol):
    def request_json(
        self,
        *,
        model: str,
        system_instruction: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...


RESEARCH_OUTPUT_SCHEMA = {
    "decision": "KEEP or SKIP",
    "reason": "short reason",
    "topic": "topic or empty for SKIP",
    "source": "source name",
    "url": "source URL",
    "published_at": "source date or empty string",
    "notes": [
        {
            "claim": "source-supported fact",
            "evidence": "concise evidence summary",
            "why_it_matters": "commercial or technical relevance",
            "confidence": "number from 0 to 1",
        }
    ],
}

RESEARCHER_INSTRUCTION = """
你是 Researcher（研究员），只做半导体行业资料判断和证据提取，不写成稿。
根据文章元数据、原始正文和 editorial scope 判断相关性。
语义无关时输出 decision=SKIP；相关时输出 decision=KEEP 和 1-4 条原文可支持的笔记。
不要补充原文之外的数字、市场份额、因果、预测或背景知识。
API、认证、quota、模型、网络或解析失败不是 SKIP，必须由程序异常处理。
严格返回符合 output_schema 的 JSON 对象。
""".strip()


class ResearcherNode:
    def __init__(
        self,
        client: JSONClient,
        model: str,
        extractor: Callable[[str], str],
    ) -> None:
        self._client = client
        self._model = model
        self._extractor = extractor

    def __call__(self, state: GraphState) -> dict[str, Any]:
        article = state["article"]
        raw_content = self._extractor(article["url"])
        result = self._client.request_json(
            model=self._model,
            system_instruction=RESEARCHER_INSTRUCTION,
            payload={
                "article_metadata": article,
                "raw_content": raw_content[:12000],
                "editorial_scope": list(EDITORIAL_SCOPE),
                "output_schema": RESEARCH_OUTPUT_SCHEMA,
            },
        )
        # Provenance is deterministic metadata, not a model-authored field.
        result["source"] = article["source"]
        result["url"] = article["url"]
        result["published_at"] = article.get("published_at", "")
        notes = validate_research_notes(result)
        return {
            "research_notes": notes,
            "status": "SKIP" if notes["decision"] == "SKIP" else "RESEARCHED",
        }
