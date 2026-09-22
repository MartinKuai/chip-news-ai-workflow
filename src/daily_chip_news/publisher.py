"""Deterministic Telegram publishing, outside the AI nodes."""

from __future__ import annotations

from typing import Any

import requests

from .schemas import GraphState


class PublisherError(RuntimeError):
    """Raised when an approved item cannot be delivered."""

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


class TelegramPublisher:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 15.0,
        enabled: bool = True,
        session: requests.Session | None = None,
        logger: Any = print,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout
        self._enabled = enabled
        self._session = session or requests.Session()
        self._logger = logger

    def publish(self, message: str, source_url: str) -> None:
        self._deliver(f"{message}\n\n原文：{source_url}")

    def publish_alert(self, message: str) -> None:
        """Send an operational alert without a Gemini call or source URL."""
        self._deliver(message)

    def _deliver(self, final_text: str) -> None:
        if not self._enabled:
            self._log(
                f"Telegram delivery skipped | reason=publish-disabled "
                f"| chars={len(final_text)}"
            )
            return
        markdown_payload = {
            "chat_id": self._chat_id,
            "text": final_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        response = self._post(markdown_payload)
        if response.status_code == 200:
            return

        if response.status_code == 400:
            plain_payload = dict(markdown_payload)
            plain_payload.pop("parse_mode")
            response = self._post(plain_payload)
            if response.status_code == 200:
                return

        status_code = response.status_code
        raise PublisherError(
            f"Telegram delivery failed with HTTP {status_code}",
            status_code=status_code,
            global_failure=status_code in {400, 401, 403, 404},
            transient=status_code == 429 or status_code >= 500,
        )

    def _post(self, payload: dict[str, Any]) -> requests.Response:
        try:
            return self._session.post(self._url, json=payload, timeout=self._timeout)
        except requests.RequestException:
            # Do not include the exception because request URLs contain the bot token.
            raise PublisherError(
                "Telegram network request failed", transient=True
            ) from None

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)


class PublisherNode:
    """Graph adapter that enforces the PASS-only publication invariant."""

    def __init__(self, publisher: TelegramPublisher) -> None:
        self._publisher = publisher

    def __call__(self, state: GraphState) -> dict[str, Any]:
        review = state.get("review", {})
        if review.get("status") != "PASS" or state.get("status") != "PASS":
            raise PublisherError(
                "Publisher received an item without Reviewer PASS",
                global_failure=True,
            )
        draft = state["draft"]
        candidate = state["candidate"]
        self._publisher.publish(draft["telegram_copy"], candidate["url"])
        return {"published": True, "status": "PASS"}
