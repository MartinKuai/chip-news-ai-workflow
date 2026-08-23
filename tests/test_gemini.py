from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from daily_chip_news.gemini import GeminiAPIError, GeminiClient, GeminiResponseError


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class GeminiClientTests(unittest.TestCase):
    def test_api_key_is_in_header_not_url(self) -> None:
        response = FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}]},
        )
        session = FakeSession(response)
        key = "sensitive-test-key"
        result = GeminiClient(key, session=session).request_json(
            model="model-a", system_instruction="role", payload={"input": "value"}
        )
        url, kwargs = session.calls[0]
        self.assertEqual({"ok": True}, result)
        self.assertNotIn(key, url)
        self.assertNotIn("?key=", url)
        self.assertEqual(key, kwargs["headers"]["x-goog-api-key"])

    def test_authentication_error_is_not_retried(self) -> None:
        session = FakeSession(FakeResponse(401, {}))
        with self.assertRaisesRegex(GeminiAPIError, "HTTP 401") as context:
            GeminiClient("secret", session=session, max_attempts=3).request_json(
                model="model-a", system_instruction="role", payload={}
            )
        self.assertEqual(1, len(session.calls))
        self.assertTrue(context.exception.global_failure)
        self.assertFalse(context.exception.transient)

    def test_server_error_is_transient_after_bounded_retries(self) -> None:
        session = FakeSession(
            FakeResponse(503, {}), FakeResponse(503, {}), FakeResponse(503, {})
        )
        with self.assertRaises(GeminiAPIError) as context:
            GeminiClient(
                "secret", session=session, max_attempts=3, sleep=lambda seconds: None
            ).request_json(model="model-a", system_instruction="role", payload={})
        self.assertEqual(3, len(session.calls))
        self.assertFalse(context.exception.global_failure)
        self.assertTrue(context.exception.transient)

    def test_rate_limit_is_transient_until_batch_circuit_opens(self) -> None:
        session = FakeSession(FakeResponse(429, {}))
        with self.assertRaises(GeminiAPIError) as context:
            GeminiClient("secret", session=session, max_attempts=1).request_json(
                model="model-a", system_instruction="role", payload={}
            )
        self.assertFalse(context.exception.global_failure)
        self.assertTrue(context.exception.transient)

    def test_invalid_json_is_explicit(self) -> None:
        session = FakeSession(
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]},
            )
        )
        with self.assertRaises(GeminiResponseError):
            GeminiClient("secret", session=session).request_json(
                model="model-a", system_instruction="role", payload={}
            )


if __name__ == "__main__":
    unittest.main()
