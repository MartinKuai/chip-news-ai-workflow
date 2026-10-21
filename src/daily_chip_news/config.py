"""Single configuration entry point for the news production pipeline.

Every tunable value used by the pipeline lives here: Gemini credentials and
per-node generation profiles (model / thinking / output tokens), source feeds,
candidate limits, retry and timeout configuration, and publishing settings
(website publishing by default, Telegram behind ``PUBLISH_TELEGRAM``).
Local runs read the git-ignored ``.env``; GitHub Actions injects Repository
Secrets and Variables with the same names.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is absent or invalid."""


DEFAULT_SOURCES = (
    "https://openai.com/news/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://blogs.nvidia.com/feed/",
    "https://huggingface.co/blog/feed.xml",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://hnrss.org/newest?points=100",
)

EDITORIAL_SCOPE = (
    "foundation models, LLMs and product launches",
    "enterprise AI applications, solutions and agent deployments",
    "AI accelerators, GPUs, datacenters and cloud compute",
    "AI funding, acquisitions, regulation and policy",
    "open-source AI models, developer tools, standards and protocols (such as MCP)",
)

EDITORIAL_BRIEF = {
    "audience": "关注前沿 AI、大模型生态与企业落地的中文商业与技术读者",
    "goal": "快速说明发生了什么、关键事实是什么、为什么重要",
    "style": (
        "中文、专业、克制、事实优先，"
        "专有名词保留英文原名（如 Claude、Gemini、MCP），不加评论与预测"
    ),
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

SITE_PUBLISH_MODES = frozenset({"github", "local", "off"})
DEFAULT_SITE_REPO = "MartinKuai/martinkuai"
DEFAULT_SITE_BRANCH = "main"
DEFAULT_SITE_NEWS_DIR = "data/news"
DEFAULT_SITE_LOCAL_DIR = "output/site"
# First scheduled run of the workflow: 2026-01-12 (stats.json "since").
DEFAULT_SITE_STATS_SINCE = "2026-01"
DEFAULT_SITE_STATS_WORKFLOW = "daily_news.yml"
DEFAULT_BRIEF_NAME = "简报"

_REPO_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]+")
_MONTH_PATTERN = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")


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
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
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


def _optional_secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if value.lower().startswith(("your_", "placeholder", "changeme")):
        return ""
    return value


def _choice(
    env: Mapping[str, str], name: str, default: str, options: frozenset[str]
) -> str:
    raw = env.get(name, default).strip().lower() or default
    if raw not in options:
        raise ConfigError(f"{name} must be one of: {', '.join(sorted(options))}")
    return raw


def _matching(
    env: Mapping[str, str], name: str, default: str, pattern: re.Pattern[str]
) -> str:
    raw = env.get(name, default).strip() or default
    if not pattern.fullmatch(raw) or ".." in raw:
        raise ConfigError(f"{name} has an invalid value")
    return raw


def _repo_path(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name, default).strip().strip("/") or default
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in raw:
        raise ConfigError(f"{name} must be a relative path inside the repository")
    return raw


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
    """Per-node model routing and generation budget, independent of provider."""

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
    # Telegram is optional since the brief moved to the website; the token and
    # chat id are required only when PUBLISH_TELEGRAM=1.
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    publish_enabled: bool = True
    publish_telegram: bool = False
    telegram_timeout_seconds: float = 15.0
    # Website publishing (BRIEF 7.2): data/news/YYYY-MM-DD.json + stats.json.
    site_publish_mode: str = "github"
    site_repo: str = DEFAULT_SITE_REPO
    site_repo_branch: str = DEFAULT_SITE_BRANCH
    site_repo_token: str = field(default="", repr=False)
    site_news_dir: str = DEFAULT_SITE_NEWS_DIR
    site_local_dir: str = DEFAULT_SITE_LOCAL_DIR
    site_timeout_seconds: float = 20.0
    site_stats_since: str = DEFAULT_SITE_STATS_SINCE
    site_stats_workflow: str = DEFAULT_SITE_STATS_WORKFLOW
    brief_name: str = DEFAULT_BRIEF_NAME
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
    gemini_min_interval_seconds: float = 15.0
    gemini_daily_request_limit: int = 18
    gemini_quota_ledger_path: str = ""
    gemini_quota_ledger_required: bool = False
    breaker_window_size: int = 5
    breaker_failure_threshold: int = 3
    run_budget_seconds: float = 2100.0
    summary_path: str = ""
    llm_provider: str = "gemini"
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_auth_mode: str = "bearer"
    llm_allowed_models: tuple[str, ...] = ()
    llm_fallback_models: tuple[str, ...] = ()
    llm_response_format: str = "json_object"
    llm_timeout_seconds: float = 120.0
    llm_max_attempts: int = 2
    llm_call_budget_seconds: float = 240.0
    llm_min_interval_seconds: float = 2.0

    @property
    def site_mode(self) -> str:
        """Effective website mode: ``PUBLISH_ENABLED=0`` turns GitHub into a dry-run."""
        if self.site_publish_mode == "github" and not self.publish_enabled:
            return "local"
        return self.site_publish_mode

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        publish_telegram = _bool(source, "PUBLISH_TELEGRAM", False)
        provider = _choice(
            source, "LLM_PROVIDER", "gemini", {"gemini", "openai-compatible"}
        )
        auth_mode = _choice(source, "LLM_AUTH_MODE", "bearer", {"bearer", "none"})
        settings = cls(
            gemini_api_key=(
                _required(source, "GEMINI_API_KEY")
                if provider == "gemini"
                else _optional_secret(source, "GEMINI_API_KEY")
            ),
            llm_provider=provider,
            llm_api_key=(
                _required(source, "LLM_API_KEY")
                if provider == "openai-compatible" and auth_mode == "bearer"
                else _optional_secret(source, "LLM_API_KEY")
            ),
            llm_auth_mode=auth_mode,
            llm_base_url=source.get("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_allowed_models=tuple(
                x.strip()
                for x in source.get("LLM_ALLOWED_MODELS", "").split(",")
                if x.strip()
            ),
            llm_fallback_models=tuple(
                x.strip()
                for x in source.get("LLM_FALLBACK_MODELS", "").split(",")
                if x.strip()
            ),
            llm_response_format=_choice(
                source, "LLM_RESPONSE_FORMAT", "json_object", {"json_object", "none"}
            ),
            llm_timeout_seconds=_positive_float(
                source, "LLM_TIMEOUT_SECONDS", 120, minimum=1, maximum=300
            ),
            llm_max_attempts=_positive_int(
                source, "LLM_MAX_ATTEMPTS", 2, minimum=1, maximum=5
            ),
            llm_call_budget_seconds=_positive_float(
                source, "LLM_CALL_BUDGET_SECONDS", 240, minimum=1, maximum=900
            ),
            llm_min_interval_seconds=_positive_float(
                source, "LLM_MIN_INTERVAL_SECONDS", 2, minimum=0, maximum=120
            ),
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
            telegram_bot_token=(
                _required(source, "TELEGRAM_BOT_TOKEN")
                if publish_telegram
                else _optional_secret(source, "TELEGRAM_BOT_TOKEN")
            ),
            telegram_chat_id=(
                _required(source, "TELEGRAM_CHAT_ID")
                if publish_telegram
                else _optional_secret(source, "TELEGRAM_CHAT_ID")
            ),
            publish_enabled=_bool(source, "PUBLISH_ENABLED", True),
            publish_telegram=publish_telegram,
            telegram_timeout_seconds=_positive_float(
                source, "TELEGRAM_TIMEOUT_SECONDS", 15.0, minimum=1.0, maximum=120.0
            ),
            site_publish_mode=_choice(
                source, "SITE_PUBLISH_MODE", "github", SITE_PUBLISH_MODES
            ),
            site_repo=_matching(source, "SITE_REPO", DEFAULT_SITE_REPO, _REPO_PATTERN),
            site_repo_branch=_matching(
                source, "SITE_REPO_BRANCH", DEFAULT_SITE_BRANCH, _BRANCH_PATTERN
            ),
            site_repo_token=_optional_secret(source, "SITE_REPO_TOKEN"),
            site_news_dir=_repo_path(source, "SITE_NEWS_DIR", DEFAULT_SITE_NEWS_DIR),
            site_local_dir=source.get("SITE_LOCAL_DIR", "").strip()
            or DEFAULT_SITE_LOCAL_DIR,
            site_timeout_seconds=_positive_float(
                source, "SITE_TIMEOUT_SECONDS", 20.0, minimum=1.0, maximum=120.0
            ),
            site_stats_since=_matching(
                source, "SITE_STATS_SINCE", DEFAULT_SITE_STATS_SINCE, _MONTH_PATTERN
            ),
            site_stats_workflow=_matching(
                source,
                "SITE_STATS_WORKFLOW",
                DEFAULT_SITE_STATS_WORKFLOW,
                _BRANCH_PATTERN,
            ),
            brief_name=source.get("BRIEF_NAME", "").strip() or DEFAULT_BRIEF_NAME,
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
            gemini_min_interval_seconds=_positive_float(
                source, "GEMINI_MIN_INTERVAL_SECONDS", 15.0, minimum=1.0, maximum=120.0
            ),
            gemini_daily_request_limit=_positive_int(
                source, "GEMINI_DAILY_REQUEST_LIMIT", 18, minimum=1, maximum=10000
            ),
            gemini_quota_ledger_path=source.get("GEMINI_QUOTA_LEDGER_PATH", "").strip(),
            gemini_quota_ledger_required=_bool(
                source, "GEMINI_QUOTA_LEDGER_REQUIRED", False
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
        if provider == "openai-compatible":
            try:
                parts = urlsplit(settings.llm_base_url)
                valid = (
                    parts.scheme == "https"
                    and bool(parts.hostname)
                    and not (
                        parts.username
                        or parts.password
                        or parts.query
                        or parts.fragment
                    )
                    and not any(c.isspace() for c in settings.llm_base_url)
                    and parts.port != 0
                )
            except ValueError:
                valid = False
            if not valid:
                raise ConfigError(
                    "LLM_BASE_URL must be HTTPS without credentials or query"
                )
            selected = {
                settings.researcher.model,
                settings.writer.model,
                settings.reviewer.model,
                *settings.llm_fallback_models,
            }
            if not settings.llm_allowed_models or not selected.issubset(
                settings.llm_allowed_models
            ):
                raise ConfigError(
                    "Every primary and fallback model must be in LLM_ALLOWED_MODELS"
                )
        if settings.site_mode == "github" and not settings.site_repo_token:
            raise ConfigError(
                "SITE_REPO_TOKEN is not configured "
                "(set SITE_PUBLISH_MODE=local for a dry-run or off to disable)"
            )
        return settings
