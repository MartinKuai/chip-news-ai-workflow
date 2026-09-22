"""Unified failure taxonomy: candidate-level vs service-level vs run-level."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .gemini import GeminiAPIError, GeminiResponseError, GeminiTruncatedResponseError
from .health import HealthEvent
from .publisher import PublisherError
from .schemas import SchemaError
from .sources import SourceError


class FailureCategory(StrEnum):
    """Machine-readable failure classes used by routing, breaker and summary."""

    TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
    TRANSIENT_SERVER = "TRANSIENT_SERVER"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    MODEL_RESPONSE_TRUNCATED = "MODEL_RESPONSE_TRUNCATED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
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

AI_STAGES = frozenset({"researcher", "writer", "reviewer"})


@dataclass(frozen=True)
class FailureInfo:
    """Routing flags derived from one node exception."""

    category: FailureCategory
    transient: bool
    # Stop processing further candidates (configuration errors, program faults).
    stop_run: bool
    # The run is FAILED even when candidates were already published.
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
        # but stay partial successes when candidates were already published.
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
    """Return the breaker event for one node exception, or None."""
    if stage not in AI_STAGES:
        return None
    if info.category in TRANSIENT_CATEGORIES:
        return HealthEvent.TRANSIENT_FAILURE
    if info.category in AI_ARTICLE_CATEGORIES:
        return HealthEvent.NON_TRANSIENT_FAILURE
    return None


def record_health_outcome(
    recorder,
    *,
    stage: str,
    cause: Exception | None = None,
) -> None:
    """Record exactly one breaker event for one logical AI call.

    ``cause is None`` means the call (including local schema validation)
    succeeded. Failures that are not Gemini service signals, such as a source
    extraction error, record nothing.
    """
    if recorder is None:
        return
    if cause is None:
        recorder(HealthEvent.SUCCESS)
        return
    event = health_event_for(stage, classify_failure(stage, cause))
    if event is not None:
        recorder(event)
