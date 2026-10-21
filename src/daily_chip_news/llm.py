"""Provider-neutral node interface and failure categories."""

from __future__ import annotations

from typing import Any, Protocol


class LLMError(RuntimeError):
    """A model service or output failure."""


class LLMAPIError(LLMError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        global_failure: bool = False,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.global_failure = global_failure
        self.transient = transient


class LLMResponseError(LLMError):
    """The service did not return a usable JSON object."""


class LLMTruncatedResponseError(LLMResponseError):
    """The output token budget was exhausted."""


class JSONClient(Protocol):
    def request_json(
        self,
        *,
        model: str,
        system_instruction: str,
        payload: dict[str, Any],
        purpose: str = "",
        output_schema: dict[str, Any] | None = None,
        thinking_level: str = "",
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]: ...
