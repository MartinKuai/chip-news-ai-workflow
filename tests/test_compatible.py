from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from _support import (
    CANDIDATE,
    DRAFT,
    FakeResponse,
    FakeSession,
    make_settings,
    research_output,
    review,
)
from test_config import BASE_ENV

from daily_chip_news.compatible import CompatibleClient
from daily_chip_news.config import ConfigError, Settings
from daily_chip_news.errors import FailureCategory, classify_failure
from daily_chip_news.llm import LLMAPIError, LLMResponseError, LLMTruncatedResponseError
from daily_chip_news.metrics import RunMetrics
from daily_chip_news.runner import run_daily
from daily_chip_news.website import LocalSiteStore, SitePublisher


def response(value=None, *, content=None, finish="stop"):
    return FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": content
                        if content is not None
                        else json.dumps(value),
                        "reasoning_content": "DO NOT EXPORT REASONING",
                    },
                    "finish_reason": finish,
                }
            ]
        },
    )


class Clock:
    def __init__(self):
        self.time = 0
        self.waits = []

    def now(self):
        return self.time

    def sleep(self, delay):
        self.waits.append(delay)
        self.time += delay


def client(*responses, **overrides):
    clock = Clock()
    session = FakeSession(*responses)
    values = dict(
        allowed_models=("free-a", "free-b"),
        session=session,
        now=clock.now,
        sleep=clock.sleep,
        min_interval=0,
    )
    values.update(overrides)
    instance = CompatibleClient("https://gateway.example/v1", "fictional-key", **values)
    return instance, session, clock


def invoke(instance, **overrides):
    values = dict(
        model="free-a",
        system_instruction="test instruction",
        payload={"task": "JSON"},
        max_output_tokens=100,
    )
    values.update(overrides)
    return instance.request_json(**values)


class CompatibleClientTests(unittest.TestCase):
    def test_public_mode_does_not_send_any_authorization(self):
        instance, session, _ = client(response({"ok": True}), auth_mode="none")
        invoke(instance)
        self.assertNotIn("Authorization", session.calls[0][2]["headers"])
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)

    def test_chat_protocol_does_not_send_gemini_fields_or_read_reasoning(self):
        instance, session, _ = client(response({"ok": True}))
        self.assertEqual({"ok": True}, invoke(instance, thinking_level="high"))
        call = session.calls[0]
        self.assertEqual("https://gateway.example/v1/chat/completions", call[1])
        body = call[2]["json"]
        self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertEqual(100, body["max_tokens"])
        self.assertNotIn("thinkingConfig", body)
        self.assertNotIn("reasoning_effort", body)
        self.assertFalse(call[2]["allow_redirects"])

    def test_unlisted_primary_or_fallback_stops_before_http(self):
        for options in [{"model": "paid-model"}, {}]:
            instance, session, _ = client(fallback_models=("paid-model",))
            with self.assertRaises(LLMAPIError):
                invoke(instance, **options)
            self.assertEqual([], session.calls)

    def test_only_allowlisted_model_is_used_for_transient_fallback(self):
        metrics = RunMetrics()
        instance, session, _ = client(
            FakeResponse(503),
            response({"ok": True}),
            fallback_models=("free-b",),
            max_attempts=1,
            metrics=metrics,
        )
        self.assertTrue(invoke(instance)["ok"])
        self.assertEqual(
            ["free-a", "free-b"], [c[2]["json"]["model"] for c in session.calls]
        )
        self.assertEqual(1, metrics.model_fallbacks)

    def test_auth_redirect_and_bad_request_do_not_retry_or_fallback(self):
        for status in (301, 400, 401, 402, 403, 404):
            with self.subTest(status=status):
                instance, session, _ = client(
                    FakeResponse(status), fallback_models=("free-b",)
                )
                with self.assertRaises(LLMAPIError) as context:
                    invoke(instance)
                self.assertEqual(status, context.exception.status_code)
                self.assertEqual(1, len(session.calls))
                self.assertNotIn("fictional-key", str(context.exception))

    def test_rate_limit_wait_is_respected_across_models(self):
        instance, session, clock = client(
            FakeResponse(429, headers={"Retry-After": "7"}),
            response({"ok": True}),
            fallback_models=("free-b",),
            max_attempts=1,
        )
        invoke(instance)
        self.assertEqual([7], clock.waits)
        self.assertEqual(2, len(session.calls))

    def test_deadline_prevents_retry_fallback_and_oversized_wait(self):
        instance, session, clock = client(
            FakeResponse(429, headers={"Retry-After": "500"}),
            fallback_models=("free-b",),
            call_budget_seconds=10,
        )
        with self.assertRaises(LLMAPIError):
            invoke(instance)
        self.assertEqual(1, len(session.calls))
        self.assertEqual([], clock.waits)

    def test_timeout_uses_run_budget_and_network_error_does_not_leak(self):
        instance, session, _ = client(response({"ok": True}), run_deadline=3)
        invoke(instance)
        self.assertEqual(3, session.calls[0][2]["timeout"])
        instance, session, _ = client(
            requests.ConnectionError("fictional-key private URL"), max_attempts=1
        )
        with self.assertRaises(LLMAPIError) as context:
            invoke(instance)
        self.assertNotIn("fictional-key", str(context.exception))
        self.assertNotIn("private URL", str(context.exception))

    def test_fenced_json_and_format_off(self):
        instance, session, _ = client(
            response(content='```json\n{"ok":true}\n```'), response_format="none"
        )
        self.assertTrue(invoke(instance)["ok"])
        self.assertNotIn("response_format", session.calls[0][2]["json"])

    def test_one_format_repair_shares_original_budget(self):
        metrics = RunMetrics()
        instance, session, _ = client(
            response(content="broken JSON"), response({"ok": True}), metrics=metrics
        )
        self.assertTrue(
            invoke(instance, output_schema={"type": "OBJECT"}, purpose="writer")["ok"]
        )
        self.assertEqual(2, len(session.calls))
        self.assertEqual(1, metrics.repairs_for("writer"))

    def test_invalid_response_and_truncation_are_not_approvals(self):
        for reply, expected in [
            (FakeResponse(200, {}), LLMResponseError),
            (response({"ok": True}, finish="length"), LLMTruncatedResponseError),
            (response(content="[]"), LLMResponseError),
            (response({"ok": True}, finish="content_filter"), LLMResponseError),
        ]:
            with self.subTest(expected=expected):
                instance, session, _ = client(reply)
                with self.assertRaises(expected):
                    invoke(instance)
                self.assertEqual(1, len(session.calls))

    def test_generic_failures_use_existing_breaker_taxonomy(self):
        self.assertEqual(
            FailureCategory.TRANSIENT_RATE_LIMIT,
            classify_failure("writer", LLMAPIError("safe", status_code=429)).category,
        )
        self.assertEqual(
            FailureCategory.MODEL_RESPONSE_TRUNCATED,
            classify_failure("writer", LLMTruncatedResponseError("safe")).category,
        )


def compatible_env(**overrides):
    values = {
        **BASE_ENV,
        "GEMINI_API_KEY": "",
        "LLM_PROVIDER": "openai-compatible",
        "LLM_API_KEY": "fictional-key",
        "LLM_BASE_URL": "https://gateway.example/v1/",
        "LLM_ALLOWED_MODELS": "researcher-model,writer-model,reviewer-model,free-b",
    }
    values.update(overrides)
    return values


class CompatibleConfigTests(unittest.TestCase):
    def test_explicit_public_free_mode_needs_no_key(self):
        settings = Settings.from_env(
            compatible_env(LLM_AUTH_MODE="none", LLM_API_KEY="")
        )
        self.assertEqual("", settings.llm_api_key)
        self.assertEqual("none", settings.llm_auth_mode)

    def test_generic_provider_needs_no_gemini_key(self):
        settings = Settings.from_env(compatible_env(LLM_FALLBACK_MODELS="free-b"))
        self.assertEqual("", settings.gemini_api_key)
        self.assertEqual("https://gateway.example/v1", settings.llm_base_url)
        self.assertNotIn("fictional-key", repr(settings))

    def test_missing_key_base_or_allowlist_is_configuration_error(self):
        for changes in [
            {"LLM_API_KEY": ""},
            {"LLM_ALLOWED_MODELS": ""},
            {"LLM_ALLOWED_MODELS": "writer-model,reviewer-model"},
            {"LLM_FALLBACK_MODELS": "paid-model"},
            {"LLM_PROVIDER": "unknown"},
            {"LLM_RESPONSE_FORMAT": "unknown"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                Settings.from_env(compatible_env(**changes))

    def test_untrusted_endpoint_is_rejected_without_echoing_credentials(self):
        for url in [
            "",
            "http://gateway.example/v1",
            "https://user:private@gateway.example/v1",
            "https://gateway.example/v1?key=private",
            "https://gateway.example:bad/v1",
        ]:
            with self.subTest(url=url), self.assertRaises(ConfigError) as context:
                Settings.from_env(compatible_env(LLM_BASE_URL=url))
            self.assertNotIn("private", str(context.exception))


class CompatiblePipelineTests(unittest.TestCase):
    def test_real_graph_and_trace_use_configured_transport_without_gemini_ledger(self):
        settings = make_settings(
            llm_provider="openai-compatible",
            llm_api_key="fictional-key",
            llm_base_url="https://gateway.example/v1",
            llm_allowed_models=("researcher-model", "writer-model", "reviewer-model"),
            llm_min_interval_seconds=0,
            max_candidate_age_hours=0,
            gemini_quota_ledger_required=True,
            gemini_quota_ledger_path="missing-ledger",
        )
        replies = iter(
            [response(research_output()), response(DRAFT), response(review("PASS"))]
        )
        calls = []

        def post(session, url, **kwargs):
            calls.append((url, kwargs))
            return next(replies)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = SitePublisher(LocalSiteStore(root), logger=None)
            with (
                patch("requests.Session.post", post),
                patch(
                    "daily_chip_news.graph.ArticleExtractor",
                    return_value=lambda url: "示例正文。" * 60,
                ),
                patch(
                    "daily_chip_news.graph.QuotaGuard",
                    side_effect=AssertionError("Gemini ledger used"),
                ),
            ):
                result = run_daily(
                    settings, candidates=[CANDIDATE], site_publisher=site
                )
            traces = list((root / "data/news").glob("*.trace.json"))
            document = json.loads(traces[0].read_text(encoding="utf-8"))
            self.assertEqual(
                {"fetched": 1, "kept": 1, "drafted": 1, "approved": 1, "published": 1},
                document["counts"],
            )
            self.assertNotIn("fictional-key", traces[0].read_text(encoding="utf-8"))
            self.assertNotIn(
                "DO NOT EXPORT REASONING", traces[0].read_text(encoding="utf-8")
            )
        self.assertEqual("SUCCESS", result["run_outcome"])
        self.assertEqual("openai-compatible", result["llm_provider"])
        self.assertEqual(3, len(calls))
        self.assertEqual(
            ["researcher-model", "writer-model", "reviewer-model"],
            [c[1]["json"]["model"] for c in calls],
        )


if __name__ == "__main__":
    unittest.main()
