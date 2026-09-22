"""Auditable Gemini pricing and token-cost math.

Every price entry carries its validity window so an unknown model or an expired
rate fails closed instead of silently spending money at a guessed rate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


class PricingUnavailableError(RuntimeError):
    """Raised when no valid price exists for a model at the given time."""


# Conservative input-token margin applied on top of countTokens so that any
# tokenizer drift can never under-project a call.
DEFAULT_INPUT_MARGIN = 0.05


@dataclass(frozen=True)
class ModelPrice:
    """One auditable pricing record for a single Gemini model."""

    model: str
    effective_from: date
    effective_until: date | None  # inclusive; None means open-ended
    input_usd_per_1m: float
    output_usd_per_1m: float  # Gemini bills thinking tokens as output
    note: str = ""

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_until is None or day <= self.effective_until


# Verified against the official Gemini Developer API pricing page on 2026-09-21.
# The 3.6/3.7 Flash introductory rate is documented both as $3.75 and $4.50 per
# 1M output tokens; the conservative (higher) figure is used until the Owner
# confirms the exact rate.
PRICING_VERIFIED_ON = date(2026, 9, 21)

PRICING: tuple[ModelPrice, ...] = (
    ModelPrice(
        "gemini-3.5-flash-lite",
        date(2026, 9, 21),
        None,
        0.30,
        2.50,
        "standard rate, verified 2026-09-21",
    ),
    ModelPrice(
        "gemini-3.6-flash",
        date(2026, 9, 21),
        date(2026, 12, 31),
        0.75,
        4.50,
        "introductory rate until 2026-12-31 (conservative 4.50 vs 3.75)",
    ),
    ModelPrice(
        "gemini-3.7-flash",
        date(2026, 9, 21),
        date(2026, 12, 31),
        0.75,
        4.50,
        "introductory rate until 2026-12-31 (conservative 4.50 vs 3.75)",
    ),
    ModelPrice(
        "gemini-3.6-flash",
        date(2027, 1, 1),
        None,
        1.50,
        7.50,
        "post-promotion standard rate",
    ),
    ModelPrice(
        "gemini-3.7-flash",
        date(2027, 1, 1),
        None,
        1.50,
        7.50,
        "post-promotion standard rate",
    ),
)


def _as_day(value: datetime | date | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.date()
    return value


def lookup_price(
    model: str,
    *,
    now: datetime | date | None = None,
    pricing: Iterable[ModelPrice] | None = None,
) -> ModelPrice:
    """Return the price record covering ``now`` or fail closed."""
    clean_model = (model or "").strip()
    if not clean_model:
        raise PricingUnavailableError("Gemini model is required for pricing")
    day = _as_day(now)
    table = PRICING if pricing is None else tuple(pricing)
    for record in table:
        if record.model == clean_model and record.covers(day):
            return record
    raise PricingUnavailableError(
        f"No valid pricing for model={clean_model} on {day.isoformat()}"
    )


def input_cost_usd(prompt_tokens: int, price: ModelPrice) -> float:
    return max(0, int(prompt_tokens)) * price.input_usd_per_1m / 1_000_000


def output_cost_usd(
    candidate_tokens: int, thought_tokens: int, price: ModelPrice
) -> float:
    """Thinking tokens are billed at the output rate."""
    billable_output = max(0, int(candidate_tokens)) + max(0, int(thought_tokens))
    return billable_output * price.output_usd_per_1m / 1_000_000


def projected_call_cost_usd(
    *,
    prompt_tokens: int,
    max_output_tokens: int,
    price: ModelPrice,
    input_margin: float = DEFAULT_INPUT_MARGIN,
) -> float:
    """Worst-case cost of one billable generation: counted input + output ceiling."""
    margin = max(0.0, float(input_margin))
    projected_input = max(0, int(prompt_tokens)) * (1.0 + margin)
    projected_output = max(0, int(max_output_tokens))
    return (
        projected_input * price.input_usd_per_1m / 1_000_000
        + projected_output * price.output_usd_per_1m / 1_000_000
    )


def actual_call_cost_usd(
    *,
    prompt_tokens: int,
    candidate_tokens: int,
    thought_tokens: int,
    price: ModelPrice,
) -> float:
    return input_cost_usd(prompt_tokens, price) + output_cost_usd(
        candidate_tokens, thought_tokens, price
    )


@dataclass
class UsageTotals:
    """Accumulated token usage and cost, per call or per run."""

    prompt_tokens: int = 0
    candidate_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    billable_requests: int = 0
    cost_usd: float = 0.0

    def add(self, other: UsageTotals) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.candidate_tokens += other.candidate_tokens
        self.thought_tokens += other.thought_tokens
        self.total_tokens += other.total_tokens
        self.billable_requests += other.billable_requests
        self.cost_usd += other.cost_usd


def usage_totals_from_metadata(
    metadata: Mapping[str, Any] | None,
    price: ModelPrice,
) -> UsageTotals | None:
    """Parse one GenerateContentResponse usageMetadata into billable totals.

    Returns ``None`` when the metadata is missing or unusable so the caller can
    keep the conservative reservation instead of assuming a zero-cost request.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw_prompt = metadata.get("promptTokenCount")
    if not isinstance(raw_prompt, int) or isinstance(raw_prompt, bool):
        return None
    candidate = metadata.get("candidatesTokenCount", 0)
    thought = metadata.get("thoughtsTokenCount", 0)
    total = metadata.get("totalTokenCount")
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        return None
    if not isinstance(thought, int) or isinstance(thought, bool):
        return None
    if not isinstance(total, int) or isinstance(total, bool):
        total = raw_prompt + candidate + thought
    return UsageTotals(
        prompt_tokens=raw_prompt,
        candidate_tokens=candidate,
        thought_tokens=thought,
        total_tokens=total,
        billable_requests=1,
        cost_usd=actual_call_cost_usd(
            prompt_tokens=raw_prompt,
            candidate_tokens=candidate,
            thought_tokens=thought,
            price=price,
        ),
    )


@dataclass
class CostGuardState:
    """Mutable cost-guard bookkeeping shared by the guard and the summary."""

    triggered: bool = False
    reason: str = ""
    stopped_at_stage: str = ""
    details: dict[str, float] = field(default_factory=dict)
