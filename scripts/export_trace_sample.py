"""Run the real editorial nodes/graph offline and export a local trace sample.

All news is fictional. A scripted JSON client replaces online model calls and
an in-memory extractor replaces article HTTP. No .env or credentials are read.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from daily_chip_news.config import NodeProfile, Settings  # noqa: E402
from daily_chip_news.graph import build_editorial_graph  # noqa: E402
from daily_chip_news.nodes import ResearcherNode, ReviewerNode, WriterNode  # noqa: E402
from daily_chip_news.publisher import PublisherNode  # noqa: E402
from daily_chip_news.runner import run_daily  # noqa: E402
from daily_chip_news.sources import candidate_id  # noqa: E402
from daily_chip_news.website import (  # noqa: E402
    SITE_TZ,
    LocalSiteStore,
    RunContext,
    SitePublisher,
)


def research(decision: str = "KEEP") -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": "属于模型产品更新，原文提供了可核验的信息。"
        if decision == "KEEP"
        else "属于消费电子评测，与本次 AI 行业新闻范围无关。",
        "topic": "示例模型更新" if decision == "KEEP" else "",
        "notes": [
            {
                "claim": "示例厂商发布模型更新，原文未披露性能数字。",
                "evidence": "示例公告明确说明发布事项。",
                "why_it_matters": "为跟踪模型产品变化提供依据。",
                "confidence": 0.9,
            }
        ]
        if decision == "KEEP"
        else [],
    }


def draft(
    headline: str, *, revised: bool = False, unsupported: bool = False
) -> dict[str, Any]:
    summary = (
        "示例厂商发布模型更新，原文未披露性能数字。"
        if revised
        else ("示例厂商发布模型更新。")
    )
    if unsupported:
        summary = "示例厂商发布模型更新，性能提升 50%。"
    return {
        "headline": headline,
        "summary": summary,
        "key_facts": ["示例厂商发布模型更新"],
        "why_it_matters": "可用于跟踪模型产品更新。",
        "telegram_copy": headline + "。" + summary,
    }


def review(status: str) -> dict[str, Any]:
    problem = "摘要中的性能判断缺少研究笔记支持。"
    return {
        "status": status,
        "scores": {
            "factuality": 9 if status == "PASS" else 6,
            "relevance": 9,
            "clarity": 9,
        },
        "issues": []
        if status == "PASS"
        else [{"severity": "major", "problem": problem}],
        "revision_brief": ["删除无依据的性能判断，只保留发布事实。"]
        if status == "REVISE"
        else [],
    }


class ScriptedClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(responses)

    def request_json(self, **kwargs: Any) -> dict[str, Any]:
        return next(self.responses)


def render(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(SITE_TZ).replace(microsecond=0)
    settings = Settings(
        gemini_api_key="",
        researcher=NodeProfile(model="offline-researcher"),
        writer=NodeProfile(model="offline-writer"),
        reviewer=NodeProfile(model="offline-reviewer"),
        telegram_bot_token="",
        telegram_chat_id="",
        publish_telegram=False,
        site_publish_mode="local",
        site_local_dir=str(root),
        max_candidate_age_hours=0,
        max_candidates_per_run=6,
    )
    responses = [
        research(),
        draft("示例一：模型产品更新"),
        review("PASS"),
        research(),
        draft("示例二：待修改的模型更新", unsupported=True),
        review("REVISE"),
        draft("示例二：模型更新说明", revised=True),
        review("PASS"),
        research("SKIP"),
        research(),
        draft("示例四：证据不足的模型消息", unsupported=True),
        review("REJECT"),
        research(),
        draft("示例五：需要返工的模型消息", unsupported=True),
        review("REVISE"),
        draft("示例五：返工后的模型消息", unsupported=True),
        review("REVISE"),
    ]
    client = ScriptedClient(responses)
    graph = build_editorial_graph(
        researcher=ResearcherNode(
            client,
            settings.researcher,
            lambda url: "示例公告：示例厂商发布模型更新，未披露性能数字。" * 20,
        ),
        writer=WriterNode(client, settings.writer),
        reviewer=ReviewerNode(client, settings.reviewer),
        publisher=PublisherNode(),
        max_revisions=1,
    )
    candidates = []
    for index in range(1, 6):
        url = f"https://news.example/fictional-ai-update-{index}"
        candidates.append(
            {
                "id": candidate_id(url),
                "title": f"Fictional AI update {index}",
                "url": url,
                "source": "示例新闻源",
                "published_at": "",
                "metadata": {},
            }
        )
    site = SitePublisher(
        LocalSiteStore(root),
        run=RunContext(event="workflow_dispatch"),
        now=lambda: now,
        started_at=now,
        brief_name="AI 新闻",
    )
    return run_daily(
        settings,
        graph=graph,
        candidates=candidates,
        site_publisher=site,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/trace-sample"))
    args = parser.parse_args()
    result = render(args.output)
    print(f"Local sample completed | outcome={result['run_outcome']}")
