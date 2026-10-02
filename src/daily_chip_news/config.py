"""Single configuration entry point for the news production pipeline.

Every tunable value used by the pipeline lives here: Gemini credentials and
per-node generation profiles (model / thinking / output tokens), source feeds,
candidate limits, retry and timeout configuration, and publishing settings.
Local runs read the git-ignored ``.env``; GitHub Actions injects Repository
Secrets and Variables with the same names.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is absent or invalid."""


DEFAULT_SOURCES = (
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

THINKING_LEVELS = frozenset({"off", "default", "none", "low", "medium", "high"})


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


def _thinking_level(env: Mapping[str, str], name: str, default: str = "low") -> str:
    raw = env.get(name, default).strip().lower()
    if raw not in THINKING_LEVELS:
        options = ", ".join(sorted(THINKING_LEVELS))
        raise ConfigError(f"{name} must be one of: {options}")
    return "" if raw in {"off", "default", "none"} else raw


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean flag")


def _source_feeds(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("SOURCE_FEEDS", "").strip()
    if not raw:
        return DEFAULT_SOURCES
    feeds = tuple(
        item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()
    )
    if not feeds:
        raise ConfigError("SOURCE_FEEDS must contain at least one feed URL")
    return feeds


@dataclass(frozen=True)
class NodeProfile:
    """Per-node Gemini routing: one model plus its generation budget."""

    model: str
    thinking_level: str = "low"
    max_output_tokens: int = 3072


@dataclass(frozen=True)
class Settings:
    """Validated settings for one news production run."""

    gemini_api_key: str = field(repr=False)
    researcher: NodeProfile
    writer: NodeProfile
    reviewer: NodeProfile
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: str
    publish_enabled: bool = True
    telegram_timeout_seconds: float = 15.0
    source_feeds: tuple[str, ...] = DEFAULT_SOURCES
    articles_per_feed: int = 2
    max_candidates_per_run: int = 6
    max_candidate_age_hours: float = 72.0
    max_revisions: int = 1
    article_content_chars: int = 12000
    gemini_timeout_seconds: float = 180.0
    gemini_max_attempts: int = 3
    gemini_call_budget_seconds: float = 240.0
    gemini_structured_output: bool = True
    gemini_fallback_models: tuple[str, ...] = ()
    breaker_window_size: int = 5
    breaker_failure_threshold: int = 3
    run_budget_seconds: float = 2100.0
    summary_path: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        settings = cls(
            gemini_api_key=_required(source, "GEMINI_API_KEY"),
            researcher=NodeProfile(
                model=_required(source, "RESEARCHER_MODEL"),
                thinking_level=_thinking_level(source, "RESEARCHER_THINKING_LEVEL"),
                max_output_tokens=_positive_int(
                    source,
                    "RESEARCHER_MAX_OUTPUT_TOKENS",
                    3072,
                    minimum=512,
                    maximum=65536,
                ),
            ),
            writer=NodeProfile(
                model=_required(source, "WRITER_MODEL"),
                thinking_level=_thinking_level(source, "WRITER_THINKING_LEVEL"),
                max_output_tokens=_positive_int(
                    source,
                    "WRITER_MAX_OUTPUT_TOKENS",
                    2560,
                    minimum=1024,
                    maximum=65536,
                ),
            ),
            reviewer=NodeProfile(
                model=_required(source, "REVIEWER_MODEL"),
                thinking_level=_thinking_level(source, "REVIEWER_THINKING_LEVEL"),
                max_output_tokens=_positive_int(
                    source,
                    "REVIEWER_MAX_OUTPUT_TOKENS",
                    1024,
                    minimum=512,
                    maximum=65536,
                ),
            ),
            telegram_bot_token=_required(source, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_required(source, "TELEGRAM_CHAT_ID"),
            publish_enabled=_bool(source, "PUBLISH_ENABLED", True),
            telegram_timeout_seconds=_positive_float(
                source, "TELEGRAM_TIMEOUT_SECONDS", 15.0, minimum=1.0, maximum=120.0
            ),
            source_feeds=_source_feeds(source),
            articles_per_feed=_positive_int(
                source, "ARTICLES_PER_FEED", 2, minimum=1, maximum=10
            ),
            max_candidates_per_run=_positive_int(
                source, "MAX_CANDIDATES_PER_RUN", 6, minimum=1, maximum=20
            ),
            max_candidate_age_hours=_positive_float(
                source, "MAX_CANDIDATE_AGE_HOURS", 72.0, minimum=0.0, maximum=720.0
            ),
            max_revisions=_positive_int(
                source, "MAX_REVISIONS", 1, minimum=0, maximum=2
            ),
            article_content_chars=_positive_int(
                source, "ARTICLE_CONTENT_CHARS", 12000, minimum=2000, maximum=60000
            ),
            gemini_timeout_seconds=_positive_float(
                source, "GEMINI_TIMEOUT_SECONDS", 180.0, minimum=30.0, maximum=300.0
            ),
            gemini_max_attempts=_positive_int(
                source, "GEMINI_MAX_ATTEMPTS", 3, minimum=1, maximum=8
            ),
            gemini_call_budget_seconds=_positive_float(
                source,
                "GEMINI_CALL_BUDGET_SECONDS",
                240.0,
                minimum=60.0,
                maximum=900.0,
            ),
            gemini_structured_output=_bool(source, "GEMINI_STRUCTURED_OUTPUT", True),
            gemini_fallback_models=tuple(
                item.strip()
                for item in source.get("GEMINI_FALLBACK_MODELS", "").split(",")
                if item.strip()
            ),
            breaker_window_size=_positive_int(
                source, "BREAKER_WINDOW", 5, minimum=2, maximum=20
            ),
            breaker_failure_threshold=_positive_int(
                source, "BREAKER_THRESHOLD", 3, minimum=1, maximum=20
            ),
            run_budget_seconds=_positive_float(
                source, "RUN_BUDGET_SECONDS", 2100.0, minimum=300.0, maximum=10800.0
            ),
            summary_path=source.get("RUN_SUMMARY_PATH", "").strip(),
        )
        if settings.breaker_failure_threshold > settings.breaker_window_size:
            raise ConfigError("BREAKER_THRESHOLD must not exceed BREAKER_WINDOW")
        return settings
