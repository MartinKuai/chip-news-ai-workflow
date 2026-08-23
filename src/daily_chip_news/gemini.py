"""Minimal Gemini REST client with structured JSON output."""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.parse import quote

import requests


class GeminiError(RuntimeError):
    """Base class for Gemini failures that must stop the workflow."""


class GeminiAPIError(GeminiError):
    """Raised for network, authentication, quota, model, or server failures."""


class GeminiResponseError(GeminiError):
    """Raised when Gemini does not return the promised JSON object."""


class GeminiClient:
    """Call a node-selected Gemini model without putting the API key in a URL."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 45.0,
        max_attempts: int = 3,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._api_key = api_key
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._session = session or requests.Session()
        self._sleep = sleep

    def request_json(
        self,
        *,
        model: str,
        system_instruction: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Return one JSON object or raise an explicit infrastructure/schema error."""
        clean_model = model.strip()
        if not clean_model:
            raise ValueError("Gemini model is required")
        encoded_model = quote(clean_model, safe="-._")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{encoded_model}:generateContent"
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
            "generationConfig": {"responseMimeType": "application/json"},
        }

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.post(
                    url, headers=headers, json=body, timeout=self._timeout
                )
            except requests.RequestException as exc:
                if attempt == self._max_attempts:
                    raise GeminiAPIError("Gemini network request failed") from exc
                self._sleep(float(attempt))
                continue

            if response.status_code == 200:
                return self._decode(response)

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < self._max_attempts:
                self._sleep(float(attempt * 2))
                continue

            raise GeminiAPIError(f"Gemini API failed with HTTP {response.status_code}")

        raise GeminiAPIError("Gemini request ended without a result")

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            envelope = response.json()
            text = envelope["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GeminiResponseError(
                "Gemini returned invalid structured JSON output"
            ) from exc
        if not isinstance(result, dict):
            raise GeminiResponseError("Gemini structured output must be a JSON object")
        return result
