"""Unified failure taxonomy: article-level vs Gemini service-level vs workflow-level."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .gemini import GeminiAPIError, GeminiResponseError, GeminiTruncatedResponseError
from .health import HealthEvent
from .publisher import PublisherError
from .schemas import SchemaError
from .sources import SourceError


class FailureCategory(str, Enum):
    """Machine-readable failure classes used by routing, health and the summary."""

    TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
    TRANSIENT_SERVER = "TRANSIENT_SERVER"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    MODEL_RESPONSE_TRUNCATED = "MODEL_RESPONSE_TRUNCATED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    SOURCE_ERROR = "SOURCE_ERROR"
    PUBLISH_ERROR = "PUBLISH_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


TRANSIENT_CATEGORIES = frozenset(
    {
        FailureCategory.TRANSIENT_RATE_LIMIT,
        FailureCategory.TRANSIENT_SERVER,
        FailureCategory.TRANSIENT_NETWORK,
    }
)

AI_ARTICLE_CATEGORIES = frozenset(
    {
        FailureCategory.MODEL_RESPONSE_INVALID,
        FailureCategory.MODEL_RESPONSE_TRUNCATED,
        FailureCategory.SCHEMA_INVALID,
    }
)

AI_STAGES = frozenset({"writer", "reviewer"})


@dataclass(frozen=True)
class FailureInfo:
    """Routing flags derived from one node exception."""

    category: FailureCategory
    transient: bool
    # Stop processing further articles (configuration errors, program faults).
    stop_run: bool
    # The run is FAILED even when articles were already published.
    force_failed: bool


def classify_failure(stage: str, cause: Exception) -> FailureInfo:
    """Map a node exception to one category and its run-level routing flags."""
    if isinstance(cause, GeminiTruncatedResponseError):
        return FailureInfo(
            FailureCategory.MODEL_RESPONSE_TRUNCATED,
            transient=False,
            stop_run=False,
            force_failed=False,
        )
    if isinstance(cause, GeminiResponseError):
        return FailureInfo(
            FailureCategory.MODEL_RESPONSE_INVALID,
            transient=False,
            stop_run=False,
            force_failed=False,
        )
    if isinstance(cause, SchemaError):
        return FailureInfo(
            FailureCategory.SCHEMA_INVALID,
            transient=False,
            stop_run=False,
            force_failed=False,
        )
    if isinstance(cause, SourceError):
        return FailureInfo(
            FailureCategory.SOURCE_ERROR,
            transient=False,
            stop_run=False,
            force_failed=False,
        )
    if isinstance(cause, GeminiAPIError):
        status_code = cause.status_code
        if status_code == 429:
            return FailureInfo(
                FailureCategory.TRANSIENT_RATE_LIMIT,
                transient=True,
                stop_run=False,
                force_failed=False,
            )
        if status_code is None or 500 <= status_code < 600:
            return FailureInfo(
                FailureCategory.TRANSIENT_NETWORK
                if status_code is None
                else FailureCategory.TRANSIENT_SERVER,
                transient=True,
                stop_run=False,
                force_failed=False,
            )
        # Authentication, permission and model configuration failures stop the run
        # but stay partial successes when articles were already published.
        return FailureInfo(
            FailureCategory.CONFIG_ERROR,
            transient=False,
            stop_run=True,
            force_failed=False,
        )
    if isinstance(cause, PublisherError):
        return FailureInfo(
            FailureCategory.PUBLISH_ERROR,
            transient=cause.transient,
            stop_run=cause.global_failure,
            force_failed=False,
        )
    # Unknown exceptions are programming/runtime faults, not safe item failures.
    return FailureInfo(
        FailureCategory.UNEXPECTED_ERROR,
        transient=False,
        stop_run=True,
        force_failed=True,
    )


def health_event_for(stage: str, info: FailureInfo) -> HealthEvent | None:
    """Return the health-window event for one node exception, or None."""
    if stage not in AI_STAGES:
        return None
    if info.category in TRANSIENT_CATEGORIES:
        return HealthEvent.TRANSIENT_FAILURE
    if info.category in AI_ARTICLE_CATEGORIES:
        return HealthEvent.NON_TRANSIENT_FAILURE
    return None
