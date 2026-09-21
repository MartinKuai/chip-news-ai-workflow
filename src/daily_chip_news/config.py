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


def _positive_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return value


def _positive_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return value


def _choice(
    env: Mapping[str, str],
    name: str,
    default: str,
    *,
    allowed: frozenset[str],
) -> str:
    raw = env.get(name, default).strip().lower()
    if raw not in allowed:
        options = ", ".join(sorted(allowed))
        raise ConfigError(f"{name} must be one of: {options}")
    return "" if raw in {"off", "default", "none"} else raw


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean flag")


@dataclass(frozen=True)
class Settings:
    """Validated settings with independent model and generation routing per node."""

    gemini_api_key: str = field(repr=False)
    writer_model: str
    reviewer_model: str
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: str
    articles_per_feed: int = 2
    max_revisions: int = 1
    article_content_chars: int = 16000
    writer_thinking_level: str = "medium"
    reviewer_thinking_level: str = "low"
    writer_max_output_tokens: int = 16384
    reviewer_max_output_tokens: int = 8192
    gemini_timeout_seconds: float = 180.0
    gemini_max_attempts: int = 5
    gemini_call_budget_seconds: float = 240.0
    gemini_structured_output: bool = True
    health_window_size: int = 5
    health_failure_threshold: int = 3
    run_budget_seconds: float = 2100.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        settings = cls(
            gemini_api_key=_required(source, "GEMINI_API_KEY"),
            writer_model=_required(source, "WRITER_MODEL"),
            reviewer_model=_required(source, "REVIEWER_MODEL"),
            telegram_bot_token=_required(source, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_required(source, "TELEGRAM_CHAT_ID"),
            articles_per_feed=_positive_int(
                source, "ARTICLES_PER_FEED", 2, minimum=1, maximum=10
            ),
            max_revisions=_positive_int(
                source, "MAX_REVISIONS", 1, minimum=0, maximum=2
            ),
            article_content_chars=_positive_int(
                source, "ARTICLE_CONTENT_CHARS", 16000, minimum=2000, maximum=60000
            ),
            writer_thinking_level=_choice(
                source,
                "WRITER_THINKING_LEVEL",
                "medium",
                allowed=frozenset({"off", "default", "none", "low", "medium", "high"}),
            ),
            reviewer_thinking_level=_choice(
                source,
                "REVIEWER_THINKING_LEVEL",
                "low",
                allowed=frozenset({"off", "default", "none", "low", "medium", "high"}),
            ),
            writer_max_output_tokens=_positive_int(
                source,
                "WRITER_MAX_OUTPUT_TOKENS",
                16384,
                minimum=1024,
                maximum=65536,
            ),
            reviewer_max_output_tokens=_positive_int(
                source,
                "REVIEWER_MAX_OUTPUT_TOKENS",
                8192,
                minimum=512,
                maximum=65536,
            ),
            gemini_timeout_seconds=_positive_float(
                source,
                "GEMINI_TIMEOUT_SECONDS",
                180.0,
                minimum=30.0,
                maximum=300.0,
            ),
            gemini_max_attempts=_positive_int(
                source, "GEMINI_MAX_ATTEMPTS", 5, minimum=1, maximum=8
            ),
            gemini_call_budget_seconds=_positive_float(
                source,
                "GEMINI_CALL_BUDGET_SECONDS",
                240.0,
                minimum=60.0,
                maximum=900.0,
            ),
            gemini_structured_output=_bool(
                source, "GEMINI_STRUCTURED_OUTPUT", True
            ),
            health_window_size=_positive_int(
                source, "GEMINI_HEALTH_WINDOW", 5, minimum=2, maximum=20
            ),
            health_failure_threshold=_positive_int(
                source, "GEMINI_HEALTH_THRESHOLD", 3, minimum=1, maximum=20
            ),
            run_budget_seconds=_positive_float(
                source,
                "RUN_BUDGET_SECONDS",
                2100.0,
                minimum=300.0,
                maximum=10800.0,
            ),
        )
        if settings.health_failure_threshold > settings.health_window_size:
            raise ConfigError(
                "GEMINI_HEALTH_THRESHOLD must not exceed GEMINI_HEALTH_WINDOW"
            )
        return settings


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
