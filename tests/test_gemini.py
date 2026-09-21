from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import requests

from _support import SRC  # noqa: F401
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


def envelope(text: str, finish: str = "STOP") -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish,
            }
        ]
    }


class FakeResponse:
    def __init__(self, status_code, body=None, *, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def clock_from(values, fallback=1e9):
    iterator = iter(values)
    return lambda: next(iterator, fallback)


def make_client(session, **kwargs):
    kwargs.setdefault("max_attempts", 5)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("random_fn", lambda low, high: 1.0)
    kwargs.setdefault("now", lambda: 0.0)
    return GeminiClient("secret-key", session=session, **kwargs)


def request(client, **kwargs):
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
        now = datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp()
        target = datetime(2026, 9, 21, tzinfo=timezone.utc) + timedelta(seconds=30)
        header = target.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(30.0, retry_after_seconds(header, now=now), places=0)

    def test_invalid_values_are_ignored(self) -> None:
        self.assertIsNone(retry_after_seconds(None))
        self.assertIsNone(retry_after_seconds(""))
        self.assertIsNone(retry_after_seconds("not-a-date"))


class JsonParsingTests(unittest.TestCase):
    def test_plain_json_object(self) -> None:
        self.assertEqual({"ok": True}, parse_json_object('{"ok":true}'))

    def test_fenced_json_block(self) -> None:
        text = 'Sure!\n```json\n{"ok": true}\n```\n'
        self.assertEqual({"ok": True}, parse_json_object(text))

    def test_fenced_block_without_language(self) -> None:
        self.assertEqual({"ok": True}, parse_json_object("```\n{\"ok\":true}\n```"))

    def test_prose_without_fence_is_not_guessed(self) -> None:
        self.assertIsNone(parse_json_object("the answer is {\"ok\":true} probably"))

    def test_arrays_and_empty_text_are_rejected(self) -> None:
        self.assertIsNone(parse_json_object("[1,2]"))
        self.assertIsNone(parse_json_object(""))


class GeminiClientTests(unittest.TestCase):
    def test_api_key_is_in_header_not_url(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        result = request(make_client(session))
        url, kwargs = session.calls[0]
        self.assertEqual({"ok": True}, result)
        self.assertNotIn("secret-key", url)
        self.assertNotIn("?key=", url)
        self.assertEqual("secret-key", kwargs["headers"]["x-goog-api-key"])

    def test_authentication_error_is_not_retried(self) -> None:
        session = FakeSession(FakeResponse(401, {}))
        with self.assertRaisesRegex(GeminiAPIError, "HTTP 401") as context:
            request(make_client(session, max_attempts=3))
        self.assertEqual(1, len(session.calls))
        self.assertTrue(context.exception.global_failure)
        self.assertFalse(context.exception.transient)

    def test_server_error_is_retried_until_success(self) -> None:
        session = FakeSession(
            FakeResponse(503, {}), FakeResponse(200, envelope('{"ok":true}'))
        )
        sleeps = []
        metrics = RunMetrics()
        result = request(
            make_client(session, sleep=sleeps.append, metrics=metrics)
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(session.calls))
        self.assertEqual(1, len(sleeps))
        self.assertEqual(1, metrics.gemini_retries)
        self.assertEqual(1, metrics.transient_server)
        self.assertEqual(1, metrics.gemini_success)

    def test_network_timeout_is_retried_until_success(self) -> None:
        session = FakeSession(
            requests.Timeout("slow"), FakeResponse(200, envelope('{"ok":true}'))
        )
        sleeps = []
        metrics = RunMetrics()
        result = request(
            make_client(session, sleep=sleeps.append, metrics=metrics)
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(session.calls))
        self.assertEqual(1, metrics.transient_network)
        self.assertEqual(1, len(sleeps))

    def test_rate_limit_respects_retry_after_header(self) -> None:
        session = FakeSession(
            FakeResponse(429, {}, headers={"Retry-After": "7"}),
            FakeResponse(200, envelope('{"ok":true}')),
        )
        sleeps = []
        metrics = RunMetrics()
        request(make_client(session, sleep=sleeps.append, metrics=metrics))
        self.assertEqual([7.0], sleeps)
        self.assertEqual(1, metrics.transient_rate_limit)

    def test_rate_limit_without_retry_after_uses_exponential_backoff(self) -> None:
        session = FakeSession(
            FakeResponse(429, {}),
            FakeResponse(429, {}),
            FakeResponse(200, envelope('{"ok":true}')),
        )
        sleeps = []
        request(make_client(session, sleep=sleeps.append))
        self.assertEqual([2.0, 4.0], sleeps)

    def test_jitter_shrinks_the_backoff_window(self) -> None:
        session = FakeSession(
            FakeResponse(503, {}), FakeResponse(200, envelope('{"ok":true}'))
        )
        sleeps = []
        request(
            make_client(session, sleep=sleeps.append, random_fn=lambda low, high: 0.7)
        )
        self.assertEqual([1.4], sleeps)

    def test_retry_after_is_capped_and_bounded_by_the_call_budget(self) -> None:
        session = FakeSession(
            FakeResponse(429, {}, headers={"Retry-After": "600"}),
            FakeResponse(200, envelope('{"ok":true}')),
        )
        sleeps = []
        request(
            make_client(
                session,
                sleep=sleeps.append,
                retry_after_cap=120.0,
                call_budget_seconds=90.0,
            )
        )
        self.assertEqual([90.0], sleeps)

    def test_server_error_is_transient_after_bounded_retries(self) -> None:
        session = FakeSession(*[FakeResponse(503, {}) for _ in range(5)])
        metrics = RunMetrics()
        with self.assertRaises(GeminiAPIError) as context:
            request(make_client(session, metrics=metrics))
        self.assertEqual(5, len(session.calls))
        self.assertFalse(context.exception.global_failure)
        self.assertTrue(context.exception.transient)
        self.assertEqual(503, context.exception.status_code)
        self.assertEqual(5, metrics.transient_server)

    def test_call_budget_stops_retrying_before_attempts_run_out(self) -> None:
        session = FakeSession(FakeResponse(503, {}))
        logs = []
        client = make_client(
            session,
            call_budget_seconds=1.0,
            sleep=lambda seconds: None,
            logger=logs.append,
            now=clock_from([0.0, 0.0, 5.0, 5.0]),
        )
        with self.assertRaises(GeminiAPIError) as context:
            request(client)
        self.assertEqual(1, len(session.calls))
        self.assertTrue(context.exception.transient)
        self.assertTrue(any("reason=deadline-reached" in line for line in logs))

    def test_call_budget_reached_before_the_first_attempt(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        client = make_client(
            session, call_budget_seconds=1.0, now=clock_from([0.0, 5.0, 5.0])
        )
        with self.assertRaises(GeminiAPIError):
            request(client)
        self.assertEqual(0, len(session.calls))

    def test_retry_logs_are_diagnosable_without_leaking_secrets(self) -> None:
        session = FakeSession(
            FakeResponse(503, {}), FakeResponse(200, envelope('{"ok":true}'))
        )
        logs = []
        request(
            make_client(
                session,
                logger=logs.append,
                sleep=lambda seconds: None,
            )
        )
        joined = "\n".join(logs)
        self.assertIn("Gemini retry", joined)
        self.assertIn("attempt=1/5 failed", joined)
        self.assertIn("http=503", joined)
        self.assertIn("retry_after=no", joined)
        self.assertIn("reason=exponential-backoff", joined)
        self.assertNotIn("secret-key", joined)
        self.assertNotIn("input", joined)

    def test_timeout_is_applied_to_every_request(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        request(make_client(session, timeout=123.0))
        self.assertEqual(123.0, session.calls[0][1]["timeout"])

    def test_run_deadline_clamps_the_http_timeout(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        client = make_client(
            session, timeout=180.0, now=lambda: 0.0, run_deadline=50.0
        )
        request(client)
        self.assertEqual(50.0, session.calls[0][1]["timeout"])

    def test_run_deadline_in_the_past_skips_the_request(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        client = make_client(session, now=lambda: 100.0, run_deadline=50.0)
        with self.assertRaises(GeminiAPIError) as context:
            request(client)
        self.assertEqual(0, len(session.calls))
        self.assertTrue(context.exception.transient)

    def test_structured_output_and_token_budget_are_sent(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        request(
            make_client(session),
            output_schema=SCHEMA,
            max_output_tokens=16384,
        )
        config = session.calls[0][1]["json"]["generationConfig"]
        self.assertEqual("application/json", config["responseMimeType"])
        self.assertEqual(SCHEMA, config["responseSchema"])
        self.assertEqual(16384, config["maxOutputTokens"])

    def test_structured_output_can_be_disabled(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        request(
            make_client(session, structured_output=False),
            output_schema=SCHEMA,
        )
        config = session.calls[0][1]["json"]["generationConfig"]
        self.assertNotIn("responseSchema", config)

    def test_thinking_level_is_sent_and_can_be_disabled(self) -> None:
        session = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        request(make_client(session), thinking_level="medium")
        config = session.calls[0][1]["json"]["generationConfig"]
        self.assertEqual({"thinkingLevel": "MEDIUM"}, config["thinkingConfig"])

        session2 = FakeSession(FakeResponse(200, envelope('{"ok":true}')))
        request(make_client(session2), thinking_level="off")
        config2 = session2.calls[0][1]["json"]["generationConfig"]
        self.assertNotIn("thinkingConfig", config2)

    def test_unsupported_thinking_config_downgrades_once_per_model(self) -> None:
        session = FakeSession(
            FakeResponse(
                400,
                {},
                text='Invalid JSON payload received. Unknown name "thinkingConfig"',
            ),
            FakeResponse(200, envelope('{"ok":true}')),
            FakeResponse(200, envelope('{"ok":true}')),
        )
        metrics = RunMetrics()
        logs = []
        client = make_client(session, metrics=metrics, logger=logs.append)
        request(client, thinking_level="medium")
        self.assertNotIn(
            "thinkingConfig", session.calls[1][1]["json"]["generationConfig"]
        )
        self.assertEqual(1, metrics.thinking_downgrades)
        self.assertTrue(any("thinkingConfig rejected" in line for line in logs))

        request(client, thinking_level="medium")
        self.assertNotIn(
            "thinkingConfig", session.calls[2][1]["json"]["generationConfig"]
        )
        self.assertEqual(1, metrics.thinking_downgrades)
        self.assertEqual(3, len(session.calls))

    def test_unrelated_400_is_not_treated_as_thinking_downgrade(self) -> None:
        session = FakeSession(FakeResponse(400, {}, text="API key not valid"))
        with self.assertRaises(GeminiAPIError) as context:
            request(make_client(session), thinking_level="medium")
        self.assertTrue(context.exception.global_failure)
        self.assertEqual(1, len(session.calls))

    def test_invalid_json_triggers_one_repair_call(self) -> None:
        session = FakeSession(
            FakeResponse(200, envelope("not json at all")),
            FakeResponse(200, envelope('{"ok":true}')),
        )
        metrics = RunMetrics()
        result = request(
            make_client(session, metrics=metrics),
            output_schema=SCHEMA,
            purpose="writer",
        )
        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(session.calls))
        repair_body = session.calls[1][1]["json"]
        self.assertIn("修复", repair_body["systemInstruction"]["parts"][0]["text"])
        repair_payload = json.loads(repair_body["contents"][0]["parts"][0]["text"])
        self.assertEqual("not json at all", repair_payload["invalid_output"])
        self.assertEqual(SCHEMA, repair_payload["output_schema"])
        self.assertEqual(1, metrics.repairs_for("writer"))
        self.assertEqual(1, metrics.repair_success_for("writer"))

    def test_fenced_json_uses_no_repair_call(self) -> None:
        session = FakeSession(
            FakeResponse(200, envelope('```json\n{"ok":true}\n```'))
        )
        metrics = RunMetrics()
        result = request(make_client(session, metrics=metrics), purpose="writer")
        self.assertEqual({"ok": True}, result)
        self.assertEqual(1, len(session.calls))
        self.assertEqual(0, metrics.repairs_for("writer"))

    def test_failed_repair_raises_response_error(self) -> None:
        session = FakeSession(
            FakeResponse(200, envelope("not json")),
            FakeResponse(200, envelope("still not json")),
        )
        metrics = RunMetrics()
        with self.assertRaises(GeminiResponseError):
            request(
                make_client(session, metrics=metrics),
                output_schema=SCHEMA,
                purpose="writer",
            )
        self.assertEqual(2, len(session.calls))
        self.assertEqual(1, metrics.repairs_for("writer"))
        self.assertEqual(0, metrics.repair_success_for("writer"))

    def test_json_repair_shares_the_logical_call_deadline(self) -> None:
        session = FakeSession(
            FakeResponse(200, envelope("not json at all")),
            FakeResponse(200, envelope('{"ok":true}')),
        )
        client = make_client(
            session,
            call_budget_seconds=100.0,
            timeout=180.0,
            now=clock_from([0.0, 0.0, 90.0]),
        )
        result = request(client, output_schema=SCHEMA, purpose="writer")
        self.assertEqual({"ok": True}, result)
        self.assertEqual(100.0, session.calls[0][1]["timeout"])
        # The repair call inherits the remaining budget instead of a fresh one.
        self.assertEqual(10.0, session.calls[1][1]["timeout"])

    def test_json_repair_is_skipped_when_the_shared_deadline_passed(self) -> None:
        session = FakeSession(FakeResponse(200, envelope("not json at all")))
        client = make_client(
            session,
            call_budget_seconds=100.0,
            now=clock_from([0.0, 0.0, 500.0]),
        )
        with self.assertRaises(GeminiResponseError):
            request(client, output_schema=SCHEMA, purpose="writer")
        self.assertEqual(1, len(session.calls))

    def test_truncated_output_is_reported_without_repair(self) -> None:
        session = FakeSession(FakeResponse(200, envelope("{\"ok\": tr", "MAX_TOKENS")))
        metrics = RunMetrics()
        with self.assertRaises(GeminiTruncatedResponseError):
            request(make_client(session, metrics=metrics), output_schema=SCHEMA)
        self.assertEqual(1, len(session.calls))
        self.assertEqual(1, metrics.response_truncated)

    def test_safety_stop_is_a_response_error(self) -> None:
        session = FakeSession(FakeResponse(200, envelope("{}", "SAFETY")))
        with self.assertRaises(GeminiResponseError):
            request(make_client(session))

    def test_non_object_json_is_rejected(self) -> None:
        session = FakeSession(FakeResponse(200, envelope("[1, 2, 3]")))
        with self.assertRaises(GeminiResponseError):
            request(make_client(session))


if __name__ == "__main__":
    unittest.main()
