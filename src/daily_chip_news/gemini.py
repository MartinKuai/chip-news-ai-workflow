"""Gemini REST client: bounded retries, structured output and defensive decoding."""

from __future__ import annotations

import json
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol
from urllib.parse import quote

import requests

from .metrics import RunMetrics


class GeminiError(RuntimeError):
    """Base class for Gemini failures; the batch runner decides their scope."""


class GeminiAPIError(GeminiError):
    """Raised for network, authentication, quota, model, or server failures."""

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


class GeminiResponseError(GeminiError):
    """Raised when Gemini does not return the promised JSON object."""


class GeminiTruncatedResponseError(GeminiResponseError):
    """Raised when Gemini stopped because the output budget was exhausted."""


class JSONClient(Protocol):
    """The narrow client interface the AI nodes depend on."""

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


class _ThinkingConfigRejected(Exception):
    """Internal signal: this model rejects generationConfig.thinkingConfig."""


REPAIR_INSTRUCTION = """
你是 JSON 格式修复器。只修复 JSON 语法与外层包装，不改变任何业务内容。
禁止补充、删除、改写事实或字段含义；禁止解释；禁止使用 Markdown 代码块。
严格输出符合 output_schema 的 JSON 对象。
""".strip()

_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINKING_FIELDS = ("thinkingconfig", "thinking_level", "thinkinglevel")


def retry_after_seconds(value: str | None, *, now: float | None = None) -> float | None:
    """Parse a ``Retry-After`` header value expressed as seconds or an HTTP date."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    current = time.time() if now is None else now
    return max(0.0, target - current)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object, accepting a single fenced ```json block as a fallback."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    candidates = [stripped]
    candidates.extend(block.strip() for block in _FENCED_JSON.findall(stripped))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class GeminiClient:
    """Call a node-selected Gemini model with bounded, observable retries."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 180.0,
        max_attempts: int = 5,
        max_backoff: float = 30.0,
        backoff_base: float = 2.0,
        call_budget_seconds: float = 300.0,
        retry_after_cap: float = 120.0,
        structured_output: bool = True,
        run_deadline: float | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
        logger: Callable[[str], None] | None = None,
        metrics: RunMetrics | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if call_budget_seconds <= 0:
            raise ValueError("call_budget_seconds must be positive")
        if max_backoff <= 0 or backoff_base <= 0:
            raise ValueError("backoff values must be positive")
        if retry_after_cap <= 0:
            raise ValueError("retry_after_cap must be positive")
        self._api_key = api_key
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._max_backoff = max_backoff
        self._backoff_base = backoff_base
        self._call_budget = call_budget_seconds
        self._retry_after_cap = retry_after_cap
        self._structured_output = structured_output
        self._run_deadline = run_deadline
        self._session = session or requests.Session()
        self._sleep = sleep
        self._random = random_fn
        self._logger = logger
        self._metrics = metrics or RunMetrics()
        self._now = now
        self._unsupported_thinking: set[str] = set()

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
        """Return one JSON object or raise an explicit infrastructure/schema error."""
        clean_model = (model or "").strip()
        if not clean_model:
            raise ValueError("Gemini model is required")
        level = (thinking_level or "").strip().lower()
        if level in {"", "off", "default", "none"}:
            level = ""
        if level and clean_model in self._unsupported_thinking:
            level = ""
        # One logical call owns exactly one deadline: the initial request, every
        # HTTP retry and the optional JSON repair share it.
        deadline = self._now() + self._call_budget
        if self._run_deadline is not None:
            deadline = min(deadline, self._run_deadline)
        try:
            return self._request_with_retries(
                model=clean_model,
                system_instruction=system_instruction,
                payload=payload,
                purpose=purpose,
                output_schema=output_schema,
                thinking_level=level,
                max_output_tokens=max_output_tokens,
                allow_repair=True,
                deadline=deadline,
            )
        except _ThinkingConfigRejected:
            self._unsupported_thinking.add(clean_model)
            self._metrics.thinking_downgrades += 1
            self._log(
                f"Gemini thinkingConfig rejected | model={clean_model} "
                "| falling back to the model default for this run"
            )
            return self._request_with_retries(
                model=clean_model,
                system_instruction=system_instruction,
                payload=payload,
                purpose=purpose,
                output_schema=output_schema,
                thinking_level="",
                max_output_tokens=max_output_tokens,
                allow_repair=True,
                deadline=deadline,
            )

    def _request_with_retries(
        self,
        *,
        model: str,
        system_instruction: str,
        payload: dict[str, Any],
        purpose: str,
        output_schema: dict[str, Any] | None,
        thinking_level: str,
        max_output_tokens: int | None,
        allow_repair: bool,
        deadline: float,
    ) -> dict[str, Any]:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(model, safe='-._')}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        body = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            )
                        }
                    ],
                }
            ],
            "generationConfig": self._generation_config(
                output_schema, thinking_level, max_output_tokens
            ),
        }
        last_error: GeminiAPIError | None = None

        for attempt in range(1, self._max_attempts + 1):
            remaining = deadline - self._now()
            if remaining <= 0:
                self._log(
                    f"Gemini deadline reached | purpose={purpose or 'n/a'} "
                    f"| attempt={attempt}/{self._max_attempts} | reason=call-budget-exhausted"
                )
                raise last_error or GeminiAPIError(
                    "Gemini request deadline reached", transient=True
                )
            self._metrics.gemini_requests += 1
            try:
                response = self._session.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=max(1.0, min(self._timeout, remaining)),
                )
            except requests.RequestException:
                self._metrics.transient_network += 1
                last_error = GeminiAPIError(
                    "Gemini network request failed", transient=True
                )
                if attempt < self._max_attempts and self._sleep_before_retry(
                    attempt=attempt,
                    purpose=purpose,
                    status_code=None,
                    retry_after=None,
                    deadline=deadline,
                ):
                    continue
                self._log_give_up(
                    attempt=attempt,
                    purpose=purpose,
                    status_code=None,
                    retry_after=None,
                    deadline=deadline,
                )
                raise last_error

            if response.status_code == 200:
                self._metrics.gemini_success += 1
                return self._decode(
                    response,
                    model=model,
                    system_instruction=system_instruction,
                    payload=payload,
                    purpose=purpose,
                    output_schema=output_schema,
                    thinking_level=thinking_level,
                    max_output_tokens=max_output_tokens,
                    allow_repair=allow_repair,
                    deadline=deadline,
                )

            if (
                response.status_code == 400
                and thinking_level
                and self._mentions_thinking_config(response)
            ):
                raise _ThinkingConfigRejected()

            status_code = response.status_code
            retryable = status_code == 429 or 500 <= status_code < 600
            if retryable:
                if status_code == 429:
                    self._metrics.transient_rate_limit += 1
                else:
                    self._metrics.transient_server += 1
                last_error = GeminiAPIError(
                    f"Gemini API failed with HTTP {status_code}",
                    status_code=status_code,
                    transient=True,
                )
                retry_after = self._header_retry_after(response)
                if attempt < self._max_attempts and self._sleep_before_retry(
                    attempt=attempt,
                    purpose=purpose,
                    status_code=status_code,
                    retry_after=retry_after,
                    deadline=deadline,
                ):
                    continue
                self._log_give_up(
                    attempt=attempt,
                    purpose=purpose,
                    status_code=status_code,
                    retry_after=retry_after,
                    deadline=deadline,
                )
                raise last_error

            self._log(
                f"Gemini request failed | purpose={purpose or 'n/a'} "
                f"| attempt={attempt}/{self._max_attempts} | http={status_code} "
                "| category=CONFIG_ERROR | retryable=no"
            )
            raise GeminiAPIError(
                f"Gemini API failed with HTTP {status_code}",
                status_code=status_code,
                global_failure=400 <= status_code < 500 and status_code != 429,
                transient=False,
            )

        raise last_error or GeminiAPIError(
            "Gemini request ended without a result", transient=True
        )

    def _generation_config(
        self,
        output_schema: dict[str, Any] | None,
        thinking_level: str,
        max_output_tokens: int | None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"responseMimeType": "application/json"}
        if self._structured_output and output_schema:
            config["responseSchema"] = output_schema
        if max_output_tokens:
            config["maxOutputTokens"] = int(max_output_tokens)
        if thinking_level:
            config["thinkingConfig"] = {"thinkingLevel": thinking_level.upper()}
        return config

    def _decode(
        self,
        response: requests.Response,
        *,
        model: str,
        system_instruction: str,
        payload: dict[str, Any],
        purpose: str,
        output_schema: dict[str, Any] | None,
        thinking_level: str,
        max_output_tokens: int | None,
        allow_repair: bool,
        deadline: float,
    ) -> dict[str, Any]:
        try:
            envelope = response.json()
            candidate = envelope["candidates"][0]
            finish_reason = str(candidate.get("finishReason", "") or "").upper()
            text = candidate["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._metrics.response_invalid += 1
            raise GeminiResponseError(
                "Gemini returned an unreadable structured response"
            ) from exc

        if finish_reason == "MAX_TOKENS":
            self._metrics.response_truncated += 1
            raise GeminiTruncatedResponseError(
                "Gemini stopped because maxOutputTokens was exhausted"
            )
        if finish_reason and finish_reason not in {"STOP", "FINISH_REASON_UNSPECIFIED"}:
            self._metrics.response_invalid += 1
            raise GeminiResponseError(
                f"Gemini stopped with finishReason={finish_reason}"
            )

        parsed = parse_json_object(str(text))
        if parsed is not None:
            return parsed

        if allow_repair and output_schema:
            self._log(
                f"Gemini JSON repair | purpose={purpose or 'n/a'} | model={model} "
                "| reason=invalid-structured-output"
            )
            repaired = self._repair_json(
                text=str(text),
                model=model,
                output_schema=output_schema,
                purpose=purpose,
                max_output_tokens=max_output_tokens,
                deadline=deadline,
            )
            if repaired is not None:
                self._metrics.record_repair(purpose, succeeded=True)
                return repaired
            self._metrics.record_repair(purpose, succeeded=False)

        self._metrics.response_invalid += 1
        raise GeminiResponseError("Gemini returned invalid structured JSON output")

    def _repair_json(
        self,
        *,
        text: str,
        model: str,
        output_schema: dict[str, Any],
        purpose: str,
        max_output_tokens: int | None,
        deadline: float,
    ) -> dict[str, Any] | None:
        repair_payload = {
            "output_schema": output_schema,
            "invalid_output": text[:12000],
        }
        try:
            return self._request_with_retries(
                model=model,
                system_instruction=REPAIR_INSTRUCTION,
                payload=repair_payload,
                purpose=purpose,
                output_schema=output_schema,
                thinking_level="",
                max_output_tokens=max_output_tokens,
                allow_repair=False,
                deadline=deadline,
            )
        except (GeminiAPIError, GeminiResponseError):
            return None

    def _sleep_before_retry(
        self,
        *,
        attempt: int,
        purpose: str,
        status_code: int | None,
        retry_after: float | None,
        deadline: float,
    ) -> bool:
        remaining = deadline - self._now()
        if remaining <= 0:
            return False
        if retry_after is not None:
            reason = "retry-after"
            delay = min(retry_after, self._retry_after_cap)
        else:
            reason = "exponential-backoff"
            delay = min(
                self._backoff_base * (2 ** (attempt - 1)), self._max_backoff
            )
        sleep_for = max(0.0, min(delay * self._random(0.7, 1.0), remaining))
        self._metrics.gemini_retries += 1
        self._log(
            f"Gemini retry | purpose={purpose or 'n/a'} "
            f"| attempt={attempt}/{self._max_attempts} failed "
            f"| http={status_code if status_code is not None else 'none'} "
            f"| retry_after={'yes' if retry_after is not None else 'no'} "
            f"| backoff={sleep_for:.1f}s | reason={reason}"
        )
        self._sleep(sleep_for)
        return True

    def _log_give_up(
        self,
        *,
        attempt: int,
        purpose: str,
        status_code: int | None,
        retry_after: float | None,
        deadline: float,
    ) -> None:
        reason = (
            "deadline-reached"
            if self._now() >= deadline
            else "attempts-exhausted"
        )
        self._log(
            f"Gemini gave up | purpose={purpose or 'n/a'} "
            f"| attempts={attempt}/{self._max_attempts} "
            f"| http={status_code if status_code is not None else 'none'} "
            f"| retry_after={'yes' if retry_after is not None else 'no'} "
            f"| reason={reason}"
        )

    @staticmethod
    def _header_retry_after(response: requests.Response) -> float | None:
        headers = getattr(response, "headers", None) or {}
        try:
            return retry_after_seconds(headers.get("Retry-After"))
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _mentions_thinking_config(response: requests.Response) -> bool:
        """Detect a 400 that explicitly names the thinking parameter (never logged)."""
        try:
            body = str(getattr(response, "text", "") or "")[:2000].lower()
        except Exception:  # pragma: no cover - response objects without text
            return False
        return any(field in body for field in _THINKING_FIELDS)

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)
