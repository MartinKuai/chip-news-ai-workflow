"""Golden dry-run: the exact website JSON one realistic run produces.

The real graph (Researcher -> Writer -> Reviewer -> Publisher) runs with every
HTTP dependency mocked: Jina Reader returns an article body and Gemini replays
scripted structured outputs. The site publisher runs in local (dry-run) mode on
top of ``previous-stats.json`` and must write exactly the files under
``tests/fixtures/site_dry_run/expected``. All news content is mock data.

Regenerate after an intentional format change with:
    UPDATE_SITE_FIXTURE=1 python -m unittest discover -s tests -t tests \
        -p "test_site_fixture.py"
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests
from _support import gemini_response, make_settings, research_output, review
from test_smoke_pipeline import RoutedHTTP

from daily_chip_news.outcomes import RunOutcome
from daily_chip_news.runner import run_daily
from daily_chip_news.sources import candidate_id
from daily_chip_news.website import SITE_TZ, LocalSiteStore, RunContext, SitePublisher

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "site_dry_run"
PUBLISHED_AT = datetime(2026, 10, 20, 5, 1, 32, tzinfo=SITE_TZ)
RUN_ID = 39323473694


def candidate(url: str, source: str, title: str, feed_url: str) -> dict:
    return {
        "id": candidate_id(url),
        "title": title,
        "url": url,
        "source": source,
        "published_at": "2026-10-19T09:30:00+00:00",
        "metadata": {"feed_url": feed_url, "feed_title": source},
    }


CANDIDATES = [
    candidate(
        "https://chipwire.example/2026/10/advanced-packaging-capacity",
        "Example Chip Wire",
        "Foundry to expand 2.5D packaging capacity by about 60%",
        "https://chipwire.example/feed/",
    ),
    candidate(
        "https://memory.example/research/q4-ddr5-contract-prices",
        "Example Memory Report",
        "Q4 server DDR5 contract prices forecast to rise 8-13%",
        "https://memory.example/feed/",
    ),
    candidate(
        "https://www.servers.example/blog/cxl-memory-pooling-in-production",
        "Hacker News: Newest",
        "What we learned running CXL memory pooling in production",
        "https://hnrss.org/newest?points=100",
    ),
    candidate(
        "https://gadgets.example/reviews/phone-camera-shootout",
        "Example Gadget Weekly",
        "Phone camera shootout: which flagship wins?",
        "https://gadgets.example/rss",
    ),
]


def draft(headline: str, summary: str, facts: list[str], why: str) -> dict:
    return {
        "headline": headline,
        "summary": summary,
        "key_facts": facts,
        "why_it_matters": why,
        "telegram_copy": f"{headline}\n\n{summary}",
    }


PACKAGING = draft(
    "晶圆代工厂扩充 2.5D 先进封装产能，2027 年底目标提升约 60%",
    "一家晶圆代工厂表示，将在 2027 年底前把 2.5D 先进封装的月产能提升约 60%，"
    "以承接 AI 加速器订单。新增产能主要来自现有厂区改造，原文未披露投资金额。",
    ["2.5D 先进封装月产能 2027 年底前提升约 60%", "新增产能来自现有厂区改造"],
    "先进封装仍是 AI 加速器出货的瓶颈之一，产能节奏会影响交期谈判。",
)
DDR5_FIRST = draft(
    "DDR5 合约价第四季度大涨，涨势将延续到明年",
    "一家市场研究机构预计，第四季度服务器 DDR5 合约价将环比上涨 8% 至 13%，"
    "并将持续上涨到明年下半年。",
    ["第四季度服务器 DDR5 合约价预计环比上涨 8% 至 13%"],
    "内存成本上升会影响整机报价。",
)
DDR5_REVISED = draft(
    "第四季度服务器 DDR5 合约价预计环比上涨 8% 至 13%",
    "一家市场研究机构预计，第四季度服务器 DDR5 合约价将环比上涨 8% 至 13%，"
    "主要原因是 HBM 挤占了部分 DRAM 产能。PC 端 DDR5 涨幅预计略低于服务器端。",
    ["第四季度服务器 DDR5 合约价预计环比上涨 8% 至 13%", "HBM 挤占部分 DRAM 产能"],
    "内存成本上升会传导到服务器整机报价，采购窗口值得提前沟通。",
)
CXL = draft(
    "云服务商分享 CXL 内存池化的生产部署经验",
    "一家云服务商在技术博客中介绍，单个机柜内 16 台服务器通过 CXL 2.0 共享内存池，"
    "内存利用率从约 55% 提升到约 75%。"
    "文章也提到，跨节点访问延迟比本地 DRAM 高约 170 纳秒。",
    ["16 台服务器共享 CXL 2.0 内存池", "内存利用率从约 55% 提升到约 75%"],
    "CXL 从样机走向生产部署，是内存扩展和池化方案的实际参考。",
)

GEMINI_SCRIPT = [
    # 1. Packaging: KEEP -> draft -> PASS
    research_output(topic="先进封装产能"),
    PACKAGING,
    review("PASS"),
    # 2. DDR5: KEEP -> draft -> REVISE (unsupported forecast) -> revised -> PASS
    research_output(topic="DDR5 合约价"),
    DDR5_FIRST,
    review(
        "REVISE",
        issues=[{"severity": "major", "problem": "明年涨势在研究笔记中没有依据"}],
        revision_brief=["删除关于明年涨势的表述", "补充研究笔记中的涨价原因"],
    ),
    DDR5_REVISED,
    review("PASS"),
    # 3. CXL via Hacker News: KEEP -> draft -> PASS
    research_output(topic="CXL 内存池化"),
    CXL,
    review("PASS"),
    # 4. Phone cameras: off scope -> SKIP, nothing written
    {
        "decision": "SKIP",
        "reason": "消费电子评测，不在范围内",
        "topic": "",
        "notes": [],
    },
]


def render(directory: Path) -> dict:
    """Run the pipeline once in dry-run mode and write the site files."""
    news_dir = directory / "data" / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE / "previous-stats.json", news_dir / "stats.json")
    http = RoutedHTTP(*[gemini_response(payload) for payload in GEMINI_SCRIPT])
    site = SitePublisher(
        LocalSiteStore(directory),
        run=RunContext(
            repository="MartinKuai/chip-news-ai-workflow",
            run_id=RUN_ID,
            event="schedule",
        ),
        now=lambda: PUBLISHED_AT,
        logger=None,
    )
    settings = make_settings(max_candidate_age_hours=0.0)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                requests.Session, "get", lambda self, url, **kw: http.get(url, **kw)
            )
        )
        stack.enter_context(
            patch.object(
                requests.Session, "post", lambda self, url, **kw: http.post(url, **kw)
            )
        )
        return run_daily(
            settings,
            candidates=[dict(item) for item in CANDIDATES],
            alert_publisher=None,
            site_publisher=site,
        )


class SiteDryRunFixtureTests(unittest.TestCase):
    def test_dry_run_output_matches_the_committed_fixture(self) -> None:
        expected_dir = FIXTURE / "expected" / "data" / "news"
        with tempfile.TemporaryDirectory() as directory:
            result = render(Path(directory))
            produced_dir = Path(directory) / "data" / "news"
            produced = {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted(produced_dir.glob("*.json"))
            }
            if os.environ.get("UPDATE_SITE_FIXTURE") == "1":
                expected_dir.mkdir(parents=True, exist_ok=True)
                for name, text in produced.items():
                    (expected_dir / name).write_text(text, encoding="utf-8")
        self.assertEqual(RunOutcome.SUCCESS.value, result["run_outcome"])
        self.assertEqual(3, result["published"])
        self.assertEqual(1, result["skipped_not_relevant"])
        self.assertEqual(1, result["revisions"])
        expected = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(expected_dir.glob("*.json"))
        }
        self.assertEqual(["2026-10-20.json", "stats.json"], sorted(expected))
        self.assertEqual(expected, produced)

    def test_fixture_follows_the_site_contract(self) -> None:
        news = FIXTURE / "expected" / "data" / "news"
        day = json.loads((news / "2026-10-20.json").read_text(encoding="utf-8"))
        stats = json.loads((news / "stats.json").read_text(encoding="utf-8"))
        self.assertEqual(["date", "published_at", "title", "items", "run"], list(day))
        for item in day["items"]:
            self.assertEqual(["title", "summary", "source"], list(item))
            self.assertEqual(["name", "url"], list(item["source"]))
            self.assertTrue(item["source"]["url"].startswith("https://"))
        self.assertEqual(
            ["total_runs", "success_rate", "since", "updated_at"], list(stats)
        )
        self.assertEqual(day["run"]["number"], stats["total_runs"])


if __name__ == "__main__":
    unittest.main()
