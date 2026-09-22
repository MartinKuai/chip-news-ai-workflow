from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import requests
from _support import SRC, FakeResponse, FakeSession  # noqa: F401

from daily_chip_news.gemini import (
    GeminiAPIError,
    GeminiClient,
    GeminiResponseError,
    GeminiTruncatedResponseError,
    parse_json_object,
    retry_after_seconds,
)
from daily_chip_news.metrics import RunMetrics

SCHEMA = {
    "type": "OBJECT",
    "properties": {"ok": {"type": "BOOLEAN"}},
    "required": ["ok"],
}


def gemini_envelope(text: str, *, finish: str = "STOP") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "candidates": [
                {"content": {"parts": [{"text": text}]}, "finishReason": finish}
            ]
        },
    )


def clock_from(values: list[float], fallback: float = 1e9):
    iterator = iter(values)
    return lambda: next(iterator, fallback)


def make_client(session: FakeSession, **kwargs) -> GeminiClient:
    kwargs.setdefault("max_attempts", 5)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("random_fn", lambda low, high: 1.0)
    kwargs.setdefault("now", lambda: 0.0)
    return GeminiClient("secret-key", session=session, **kwargs)


def request(client: GeminiClient, **kwargs):
    arguments = {
        "model": "model-a",
        "system_instruction": "role",
        "payload": {"input": "value"},
    }
    arguments.update(kwargs)
    return client.request_json(**arguments)


class RetryAfterParsingTests(unittest.TestCase):
    def test_seconds_value_is_parsed(self) -> None:
        self.assertEqual(7.0, retry_after_seconds("7"))
        self.assertEqual(0.0, retry_after_seconds("-5"))

    def test_http_date_value_is_parsed(self) -> None:
        now = datetime(2026, 9, 21, tzinfo=UTC).timestamp()
        target = datetime(2026, 9, 21, tzinfo=UTC) + timedelta(seconds=30)
        header = target.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(30.0, retry_after_seconds(header, now=now), places=0)

    def test_invalid_values_are_ignored(self) -> None:
        self.assertIsNone(retry_after_seconds(None))
        self.assertIsNone(retry_after_seconds("not-a-date"))


class JsonParsingTests(unittest.TestCase):
    def test_plain_json_object(self) -> None:
        self.assertEqual({"ok": True}, parse_json_object('{"ok":true}'))

    def test_fenced_json_block(self) -> None:
        text = 'Sure!\n```json\n{"ok": true}\n```\n'
        self.assertEqual({"ok": True}, parse_json_object(text))

    def test_prose_without_fence_is_not_guessed(self) -> None:
        self.assertIsNone(parse_json_object('the answer is {"ok":true} probably'))

    def test_arrays_and_empty_text_are_rejected(self) -> None:
        self.assertIsNone(parse_json_object("[1,2]"))
        self.assertIsNone(parse_json_object("   "))


class RequestSerializationTests(unittest.TestCase):
    def test_request_body_and_headers(self) -> None:
        session = FakeSession(gemini_envelope('{"ok": true}'))
        request(
            make_client(session),
            output_schema=SCHEMA,
            thinking_level="low",
            max_output_tokens=1234,
        )
        method, url, kwargs = session.calls[0]
        self.assertEqual("POST", method)
        self.assertIn("models/model-a:generateContent", url)
        self.assertEqual("secret-key", kwargs["headers"]["x-goog-api-key"])
        body = kwargs["json"]
        self.assertEqual("role", body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual("user", body["contents"][0]["role"])
        config = body["generationConfig"]
        self.assertEqual("application/json", config["responseMimeType"])
        self.assertEqual(SCHEMA, config["responseSchema"])
        self.assertEqual(1234, config["maxOutputTokens"])
        self.assertEqual({"thinkingLevel": "LOW"}, config["thinkingConfig"])

    def test_structured_output_can_be_disabled(self) -> None:
        session = FakeSession(gemini_envelope('{"ok": true}'))
        request(make_client(session, structured_output=False), output_schema=SCHEMA)
        config = session.calls[0][2]["json"]["generationConfig"]
        self.assertNotIn("responseSchema", config)

    def test_constructor_validation(self) -> None:
        with self.assertRaises(ValueError):
            GeminiClient("")
        with self.assertRaises(ValueError):
            GeminiClient("key", max_attempts=0)
        with self.assertRaises(ValueError):
            GeminiClient("key", timeout=0)

    def test_model_name_is_required(self) -> None:
        with self.assertRaises(ValueError):
            request(make_client(FakeSession()), model="  ")


class SuccessTests(unittest.TestCase):
    def test_json_response_is_returned(self) -> None:
        session = FakeSession(gemini_envelope('{"ok": true}'))
        self.assertEqual({"ok": True}, request(make_client(session)))

    def test_fenced_json_response_is_accepted(self) -> None:
        session = FakeSession(gemini_envelope('```json\n{"ok": true}\n```'))
        self.assertEqual({"ok": True}, request(make_client(session)))


class RetryTests(unittest.TestCase):
    def test_server_error_is_retried_and_succeeds(self) -> None:
        metrics = RunMetrics()
        sleeps: list[float] = []
        session = FakeSession(
            FakeResponse(503, text=""), gemini_envelope('{"ok": true}')
        )
        client = make_client(
            session, metrics=metrics, sleep=sleeps.append, max_attempts=2
        )
        self.assertEqual({"ok": True}, request(client))
        self.assertEqual(1, metrics.gemini_retries)
        self.assertEqual(2, metrics.gemini_requests)
        self.assertEqual(1, metrics.gemini_success)
        self.assertEqual(1, metrics.transient_server)
        self.assertEqual(1, len(sleeps))

    def test_rate_limit_honours_retry_after(self) -> None:
        sleeps: list[float] = []
        session = FakeSession(
            FakeResponse(429, text="", headers={"Retry-After": "5"}),
            gemini_envelope('{"ok": true}'),
        )
        client = make_client(session, sleep=sleeps.append, max_attempts=2)
        request(client)
        self.assertEqual([5.0], sleeps)

    def test_transient_failure_after_all_attempts_is_raised(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(FakeResponse(503, text=""), FakeResponse(503, text=""))
        client = make_client(session, metrics=metrics, max_attempts=2)
        with self.assertRaises(GeminiAPIError) as context:
            request(client)
        self.assertTrue(context.exception.transient)
        self.assertEqual(503, context.exception.status_code)
        self.assertEqual(2, metrics.transient_server)

    def test_network_failure_is_retried_then_raised_as_transient(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(
            requests.ConnectionError("boom"), requests.ConnectionError("boom")
        )
        client = make_client(session, metrics=metrics, max_attempts=2)
        with self.assertRaises(GeminiAPIError) as context:
            request(client)
        self.assertTrue(context.exception.transient)
        self.assertEqual(2, metrics.transient_network)

    def test_non_retryable_status_is_not_retried(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(FakeResponse(400, text="bad request"))
        client = make_client(session, metrics=metrics)
        with self.assertRaises(GeminiAPIError) as context:
            request(client)
        self.assertFalse(context.exception.transient)
        self.assertTrue(context.exception.global_failure)
        self.assertEqual(1, metrics.gemini_requests)

    def test_call_budget_exhaustion_is_reported(self) -> None:
        session = FakeSession(gemini_envelope('{"ok": true}'))
        client = make_client(
            session, now=clock_from([0.0, 500.0]), call_budget_seconds=300.0
        )
        with self.assertRaises(GeminiAPIError) as context:
            request(client)
        self.assertTrue(context.exception.transient)


class RepairTests(unittest.TestCase):
    def test_invalid_json_is_repaired_through_a_normal_model_call(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(
            gemini_envelope("This is not JSON at all"),
            gemini_envelope('{"ok": true}'),
        )
        client = make_client(session, metrics=metrics)
        self.assertEqual({"ok": True}, request(client, output_schema=SCHEMA))
        self.assertEqual(1, metrics.repairs_for(""))
        self.assertEqual(1, metrics.repair_success_for(""))

    def test_failed_repair_raises_a_response_error(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(
            gemini_envelope("nonsense"),
            gemini_envelope("still nonsense"),
        )
        client = make_client(session, metrics=metrics, max_attempts=1)
        with self.assertRaises(GeminiResponseError):
            request(client, output_schema=SCHEMA)
        self.assertEqual(1, metrics.repairs_for(""))
        self.assertEqual(0, metrics.repair_success_for(""))

    def test_without_a_schema_invalid_json_is_not_repaired(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(gemini_envelope("nonsense"))
        client = make_client(session, metrics=metrics)
        with self.assertRaises(GeminiResponseError):
            request(client)
        self.assertEqual({}, metrics.json_repairs)

    def test_repair_counts_as_an_extra_request(self) -> None:
        session = FakeSession(
            gemini_envelope("nonsense"), gemini_envelope('{"ok": true}')
        )
        client = make_client(session)
        request(client, output_schema=SCHEMA)
        self.assertEqual(2, len(session.calls))


class TruncationTests(unittest.TestCase):
    def test_max_tokens_finish_reason_is_reported(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(gemini_envelope('{"ok": true}', finish="MAX_TOKENS"))
        client = make_client(session, metrics=metrics)
        with self.assertRaises(GeminiTruncatedResponseError):
            request(client)
        self.assertEqual(1, metrics.response_truncated)

    def test_blocked_finish_reason_is_a_response_error(self) -> None:
        session = FakeSession(gemini_envelope('{"ok": true}', finish="SAFETY"))
        with self.assertRaises(GeminiResponseError):
            request(make_client(session))

    def test_unreadable_envelope_is_a_response_error(self) -> None:
        session = FakeSession(FakeResponse(200, {"candidates": []}))
        with self.assertRaises(GeminiResponseError):
            request(make_client(session))


class ThinkingDowngradeTests(unittest.TestCase):
    def test_thinking_rejection_downgrades_the_run(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(
            FakeResponse(400, text="Invalid JSON payload: thinkingConfig"),
            gemini_envelope('{"ok": true}'),
        )
        client = make_client(session, metrics=metrics)
        self.assertEqual({"ok": True}, request(client, thinking_level="low"))
        self.assertEqual(1, metrics.thinking_downgrades)
        second = session.calls[1][2]["json"]["generationConfig"]
        self.assertNotIn("thinkingConfig", second)

    def test_unrelated_400_is_not_a_thinking_downgrade(self) -> None:
        metrics = RunMetrics()
        session = FakeSession(FakeResponse(400, text="API key not valid"))
        client = make_client(session, metrics=metrics)
        with self.assertRaises(GeminiAPIError):
            request(client, thinking_level="low")
        self.assertEqual(0, metrics.thinking_downgrades)
        self.assertEqual(1, len(session.calls))


if __name__ == "__main__":
    unittest.main()
