"""Deterministic Telegram publishing, outside the three AI agents."""

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
        session: requests.Session | None = None,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout
        self._session = session or requests.Session()

    def publish(self, message: str, source_url: str) -> None:
        final_text = f"{message}\n\n原文：{source_url}"
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
            return self._session.post(
                self._url, json=payload, timeout=self._timeout
            )
        except requests.RequestException:
            # Do not include the exception because request URLs contain the bot token.
            raise PublisherError(
                "Telegram network request failed", transient=True
            ) from None


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
        article = state["article"]
        self._publisher.publish(draft["telegram_copy"], article["url"])
        return {"published": True, "status": "PASS"}
