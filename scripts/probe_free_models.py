"""Explicit, keyless live probe with local-only output and fixed public input.

Does not read .env, OpenCode login files or account credentials. No RSS crawl,
Telegram call, website commit or GitHub workflow dispatch is performed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import dotenv_values  # noqa: E402

from daily_chip_news.config import Settings  # noqa: E402
from daily_chip_news.runner import run_daily  # noqa: E402
from daily_chip_news.sources import candidate_id  # noqa: E402
from daily_chip_news.website import LocalSiteStore, RunContext, SitePublisher  # noqa: E402

SOURCE_URL = "https://opencode.ai/v2/docs/console/api/inference/"
# Fixed paraphrase of the official page, checked on 2026-10-21. No launch date,
# numeric quota, SLA or benchmark result is inferred from the documentation.
PUBLIC_INPUT = (
    "OpenCode 官方 Inference API 文档说明，Console 提供兼容多种 API 的模型接口。"
    "付费模型要求 API Key，免费的聊天模型可以不提供认证头。"
    "开源模型和免费聊天模型使用 Chat Completions 接口。"
    "另一份官方模型目录列出 DeepSeek V4.1 Flash Free 和 GLM-5.3-Flash Free，"
    "两者均为限时免费，目录没有承诺固定结束日期或可用性 SLA。"
    "原文没有披露上述能力的首次上线日期、实际吞吐量或结构化输出成功率。"
)


def probe(config: Path, output: Path) -> dict:
    values = {
        k: v
        for k, v in dotenv_values(config, interpolate=False).items()
        if v is not None
    }
    # Enforce keyless, local-only behavior even if a caller passes another file.
    values.update(
        {
            "LLM_PROVIDER": "openai-compatible",
            "LLM_AUTH_MODE": "none",
            "LLM_API_KEY": "",
            "GEMINI_API_KEY": "",
            "SITE_REPO_TOKEN": "",
            "SITE_PUBLISH_MODE": "local",
            "SITE_LOCAL_DIR": str(output),
            "PUBLISH_TELEGRAM": "0",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "LLM_MAX_ATTEMPTS": "1",
            "LLM_TIMEOUT_SECONDS": "60",
            "LLM_CALL_BUDGET_SECONDS": "90",
            "MAX_CANDIDATES_PER_RUN": "1",
            "MAX_CANDIDATE_AGE_HOURS": "0",
            "RUN_BUDGET_SECONDS": "300",
        }
    )
    settings = Settings.from_env(values)
    site = SitePublisher(
        LocalSiteStore(output),
        run=RunContext(event="workflow_dispatch"),
        brief_name="AI 新闻",
    )
    candidate = {
        "id": candidate_id(SOURCE_URL),
        "title": "OpenCode documents keyless API access for free chat models",
        "source": "OpenCode 官方文档",
        "url": SOURCE_URL,
        "published_at": "",
        "metadata": {},
    }
    # The model HTTP calls and all editorial nodes/graph routing are real.
    # Only article extraction is replaced with the reviewed public fixture.
    with patch(
        "daily_chip_news.graph.ArticleExtractor", return_value=lambda url: PUBLIC_INPUT
    ):
        return run_daily(settings, candidates=[candidate], site_publisher=site)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/opencode-free.env.example")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/opencode-free-live")
    )
    args = parser.parse_args()
    result = probe(args.config, args.output)
    print(f"Keyless live probe completed | outcome={result['run_outcome']}")
    raise SystemExit(result["exit_code"])
