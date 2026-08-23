"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is absent or invalid."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value or value.lower().startswith(("your_", "placeholder", "changeme")):
        raise ConfigError(f"{name} is not configured")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    """Validated settings with independent model routing for each AI node."""

    gemini_api_key: str = field(repr=False)
    researcher_model: str
    writer_model: str
    reviewer_model: str
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: str
    articles_per_feed: int = 2
    max_revisions: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            gemini_api_key=_required(source, "GEMINI_API_KEY"),
            researcher_model=_required(source, "RESEARCHER_MODEL"),
            writer_model=_required(source, "WRITER_MODEL"),
            reviewer_model=_required(source, "REVIEWER_MODEL"),
            telegram_bot_token=_required(source, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_required(source, "TELEGRAM_CHAT_ID"),
            articles_per_feed=_positive_int(
                source, "ARTICLES_PER_FEED", 2, minimum=1
            ),
            max_revisions=_positive_int(source, "MAX_REVISIONS", 2, minimum=0),
        )


RSS_FEEDS = (
    "https://www.eetimes.com/feed/",
    "https://semiengineering.com/feed/",
    "https://www.servethehome.com/feed/",
    "https://www.trendforce.com/feed/Semiconductors.html",
    "https://hnrss.org/newest?points=100",
)

EDITORIAL_SCOPE = (
    "semiconductors",
    "foundries and process nodes",
    "memory and storage",
    "chip vendors and product launches",
    "AI infrastructure and server hardware",
    "supply-chain pricing, shortages and inventory",
    "B2B hardware technologies such as HBM, CXL and RISC-V",
)

EDITORIAL_BRIEF = {
    "audience": "关注半导体、AI 基础设施与 B2B 硬件的中文商业读者",
    "goal": "快速说明发生了什么、关键事实是什么、为什么值得关注",
    "style": "中文、简洁、克制、事实优先，不写营销腔",
}

REVIEW_RUBRIC = (
    "factual grounding",
    "source support",
    "unsupported claims",
    "commercial relevance",
    "recency",
    "clarity",
    "duplication",
    "tone",
    "length",
    "format",
)
