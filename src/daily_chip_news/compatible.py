"""Configurable Chat Completions transport with an explicit model allowlist.

No provider/model selection, subscription routing or paid fallback is inferred.
The existing node validators remain the authority for editorial JSON output.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import requests

from .gemini import REPAIR_INSTRUCTION, parse_json_object, retry_after_seconds
from .llm import LLMAPIError, LLMResponseError, LLMTruncatedResponseError
from .metrics import RunMetrics


class CompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        allowed_models: tuple[str, ...],
        auth_mode: str = "bearer",
        fallback_models: tuple[str, ...] = (),
        response_format: str = "json_object",
        timeout: float = 120,
        max_attempts: int = 2,
        call_budget_seconds: float = 240,
        min_interval: float = 2,
        run_deadline: float | None = None,
        session: requests.Session | None = None,
        metrics: RunMetrics | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._auth_mode = auth_mode
        self._allowed = frozenset(allowed_models)
        self._fallback = fallback_models
        self._format = response_format
        self._timeout = timeout
        self._attempts = max_attempts
        self._budget = call_budget_seconds
        self._interval = min_interval
        self._run_deadline = run_deadline
        self._session = session or requests.Session()
        if auth_mode == "none":
            # Public free mode must not load .netrc, ambient proxy credentials,
            # or reuse authentication on an injected session.
            self._session.trust_env = False
            self._session.auth = None
            getattr(self._session, "headers", {}).pop("Authorization", None)
        self._metrics = metrics or RunMetrics()
        self._now, self._sleep = now, sleep
        self._next_request = 0.0

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
    ) -> dict[str, Any]:
        # Thinking parameters vary by service. Do not forward Gemini-specific
        # fields, or forward reasoning text into downstream nodes/the trace.
        choices = [model]
        fallback = next((x for x in self._fallback if x != model), "")
        if fallback:
            choices.append(fallback)
        if not self._allowed or any(x not in self._allowed for x in choices):
            raise LLMAPIError(
                "Model not permitted by LLM_ALLOWED_MODELS", status_code=400
            )
        deadline = self._now() + self._budget
        if self._run_deadline is not None:
            deadline = min(deadline, self._run_deadline)
        for index, selected in enumerate(choices):
            try:
                text = self._request(
                    selected,
                    system_instruction,
                    payload,
                    max_output_tokens,
                    deadline,
                )
            except LLMAPIError as exc:
                if (
                    index + 1 < len(choices)
                    and exc.status_code is not None
                    and (exc.status_code == 429 or 500 <= exc.status_code < 600)
                    and self._now() < deadline
                ):
                    self._metrics.model_fallbacks += 1
                    continue
                raise
            parsed = parse_json_object(text)
            if parsed is not None:
                return parsed
            if output_schema:
                try:
                    repaired = self._request(
                        selected,
                        REPAIR_INSTRUCTION,
                        {
                            "output_schema": output_schema,
                            "invalid_output": text[:12000],
                        },
                        max_output_tokens,
                        deadline,
                    )
                    parsed = parse_json_object(repaired)
                except (LLMAPIError, LLMResponseError):
                    self._metrics.record_repair(purpose, succeeded=False)
                    raise
                self._metrics.record_repair(purpose, succeeded=parsed is not None)
                if parsed is not None:
                    return parsed
            self._metrics.response_invalid += 1
            raise LLMResponseError("Model returned invalid structured JSON")
        raise LLMAPIError("No permitted model completed the request")

    def _wait(self, deadline: float, *, status_code: int | None = None) -> None:
        delay = max(0, self._next_request - self._now())
        if self._now() + delay >= deadline:
            raise LLMAPIError(
                "Model call budget exhausted",
                status_code=status_code,
                transient=True,
            )
        if delay:
            self._sleep(delay)

    def _request(
        self,
        model: str,
        instruction: str,
        payload: dict[str, Any],
        max_tokens: int | None,
        deadline: float,
    ) -> str:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if self._format == "json_object":
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "daily-ai-news/1.0",
        }
        if self._auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self._api_key}"
        last_error = LLMAPIError("Model request did not complete", transient=True)
        for attempt in range(self._attempts):
            self._wait(deadline, status_code=last_error.status_code)
            self._next_request = self._now() + self._interval
            self._metrics.gemini_requests += 1  # Existing summary counter names.
            self._metrics.model_requests[model] = (
                self._metrics.model_requests.get(model, 0) + 1
            )
            try:
                response = self._session.post(
                    self._url,
                    json=body,
                    headers=headers,
                    timeout=max(0.001, min(self._timeout, deadline - self._now())),
                    allow_redirects=False,
                )
            except requests.RequestException:
                # Exception messages can contain URLs and credentials.
                self._metrics.transient_network += 1
                last_error = LLMAPIError("Model transport failed", transient=True)
                self._next_request = max(self._next_request, self._now() + 2**attempt)
            else:
                status = response.status_code
                self._metrics.http_statuses[status] = (
                    self._metrics.http_statuses.get(status, 0) + 1
                )
                if status == 200:
                    try:
                        choice = response.json()["choices"][0]
                        content = choice["message"]["content"]
                        finish = choice.get("finish_reason")
                    except (ValueError, KeyError, IndexError, TypeError):
                        self._metrics.response_invalid += 1
                        raise LLMResponseError(
                            "Invalid Chat Completions response"
                        ) from None
                    if finish == "length":
                        self._metrics.response_truncated += 1
                        raise LLMTruncatedResponseError(
                            "Model output token limit reached"
                        )
                    if finish not in {None, "stop"} or not isinstance(content, str):
                        self._metrics.response_invalid += 1
                        raise LLMResponseError(
                            "Model did not return a completed text response"
                        )
                    self._metrics.gemini_success += 1
                    return content
                transient = status == 429 or 500 <= status < 600
                last_error = LLMAPIError(
                    f"Model API failed with HTTP {status}",
                    status_code=status,
                    transient=transient,
                    global_failure=not transient,
                )
                if not transient:
                    raise last_error
                if status == 429:
                    self._metrics.transient_rate_limit += 1
                else:
                    self._metrics.transient_server += 1
                delay = retry_after_seconds((response.headers or {}).get("Retry-After"))
                delay = (
                    delay
                    if delay is not None
                    else (60 if status == 429 else 2**attempt)
                )
                self._next_request = max(self._next_request, self._now() + delay)
            if attempt + 1 < self._attempts:
                self._metrics.gemini_retries += 1
        raise last_error
