"""Application orchestration around the per-article StateGraph."""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from .config import ConfigError, Settings
from .graph import create_runtime_graph
from .sources import collect_articles


def run_daily(settings: Settings) -> dict[str, int]:
    graph = create_runtime_graph(settings)
    articles = collect_articles(settings.articles_per_feed)
    stats = {
        "candidates": len(articles),
        "skipped": 0,
        "passed": 0,
        "revised": 0,
        "held": 0,
        "published": 0,
    }
    for article in articles:
        print(f"Processing: {article['title']}")
        result = graph.invoke(
            {
                "article": article,
                "revision_brief": [],
                "revision_count": 0,
                "status": "NEW",
                "published": False,
            }
        )
        status = result["status"]
        if status == "SKIP":
            stats["skipped"] += 1
        elif status == "HOLD":
            stats["held"] += 1
        elif status == "PASS" and result.get("published"):
            stats["passed"] += 1
            stats["published"] += 1
            if result.get("revision_count", 0) > 0:
                stats["revised"] += 1
        else:
            raise RuntimeError(f"Graph ended in an invalid state: {status}")
    print("Run summary:")
    for name, value in stats.items():
        print(f"  {name}: {value}")
    return stats


def main() -> None:
    load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("Daily Chip News V2 run started")
    run_daily(settings)
