"""Run-level counters reported by the final summary."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunMetrics:
    """Gemini client counters that cannot be derived from the failure list."""

    gemini_requests: int = 0
    gemini_success: int = 0
    gemini_retries: int = 0
    transient_rate_limit: int = 0
    transient_server: int = 0
    transient_network: int = 0
    response_invalid: int = 0
    response_truncated: int = 0
    thinking_downgrades: int = 0
    model_fallbacks: int = 0
    json_repairs: dict[str, int] = field(default_factory=dict)
    json_repair_success: dict[str, int] = field(default_factory=dict)

    @property
    def transient_failures(self) -> int:
        return (
            self.transient_rate_limit + self.transient_server + self.transient_network
        )

    def record_repair(self, purpose: str, *, succeeded: bool) -> None:
        key = purpose or "n/a"
        self.json_repairs[key] = self.json_repairs.get(key, 0) + 1
        if succeeded:
            self.json_repair_success[key] = self.json_repair_success.get(key, 0) + 1

    def repairs_for(self, purpose: str) -> int:
        return self.json_repairs.get(purpose or "n/a", 0)

    def repair_success_for(self, purpose: str) -> int:
        return self.json_repair_success.get(purpose or "n/a", 0)
