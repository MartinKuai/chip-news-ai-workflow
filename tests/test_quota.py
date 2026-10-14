from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from _support import SRC, FakeResponse, FakeSession, gemini_response, make_settings
from daily_chip_news.gemini import GeminiAPIError, GeminiClient, quota_error_details
from daily_chip_news.quota import (
    QuotaGuard,
    QuotaStateError,
    QuotaStopped,
    QuotaWaitDeadline,
    quota_day,
)
from daily_chip_news.runner import run_daily
from daily_chip_news.runner import _alert_lines
from daily_chip_news.config import ConfigError, Settings
from daily_chip_news.metrics import RunMetrics
from daily_chip_news.outcomes import RunOutcome
from daily_chip_news.graph import NodeExecutionError, STATUS_PUBLISHED, STATUS_SKIP
from test_runner import FakeGraph, candidates, terminal
from test_config import env

import importlib.util

spec = importlib.util.spec_from_file_location(
    "restore_quota_ledger", SRC.parent / "scripts" / "restore_quota_ledger.py"
)
restore_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore_module)


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.wall = datetime(2026, 10, 15, 12, tzinfo=UTC)
        self.sleeps = []

    def now(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds
        self.wall += timedelta(seconds=seconds)


def quota_response(scope="PerDay", *, delay="57s"):
    return FakeResponse(
        429,
        {
            "error": {
                "message": "DO NOT LOG secret-project-id",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": f"GenerateRequests{scope}PerProjectPerModel-FreeTier"
                            }
                        ],
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": delay,
                    },
                ],
            }
        },
    )


class GuardTests(unittest.TestCase):
    def guard(self, **kwargs):
        self.clock = Clock()
        return QuotaGuard(
            now=self.clock.now,
            wall_clock=lambda: self.clock.wall,
            sleep=self.clock.sleep,
            **kwargs,
        )

    def test_pacing_is_shared_across_calls_but_independent_per_model(self):
        guard = self.guard()
        guard.before_request("a", 240)
        guard.before_request("b", 240)
        guard.before_request("a", 240)
        self.assertEqual([15.0], self.clock.sleeps)
        self.assertEqual({"a": 2, "b": 1}, guard.requests)

    def test_reservations_survive_new_processes_and_enforce_the_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.json")
            guard = self.guard(ledger_path=path, daily_limit=2)
            guard.before_request("a", 240)
            restored = QuotaGuard(
                ledger_path=path,
                daily_limit=2,
                wall_clock=lambda: self.clock.wall,
                now=self.clock.now,
                sleep=self.clock.sleep,
            )
            restored.before_request("a", 240)
            with self.assertRaises(QuotaStopped):
                restored.before_request("a", restored.now() + 240)
            self.assertEqual(2, json.loads(Path(path).read_text())["requests"]["a"])
            self.assertEqual([15.0], self.clock.sleeps)

    def test_pacific_midnight_resets_counts_and_provider_blocks(self):
        guard = self.guard(daily_limit=1)
        self.clock.wall = datetime(2026, 10, 16, 6, 59, tzinfo=UTC)
        guard.before_request("a", 240)
        guard.block_daily("a")
        self.clock.wall += timedelta(minutes=2)
        guard.before_request("a", 240)
        self.assertEqual("2026-10-16", guard.day)
        self.assertEqual({"a": 1}, guard.requests)
        self.assertEqual(set(), guard.blocked)

    def test_day_boundary_tracks_dst_not_fixed_utc_eight(self):
        self.assertEqual(
            "2026-10-15", quota_day(datetime(2026, 10, 16, 6, 59, tzinfo=UTC))
        )
        self.assertEqual("2026-10-16", quota_day(datetime(2026, 10, 16, 7, tzinfo=UTC)))
        self.assertEqual(
            "2026-12-15", quota_day(datetime(2026, 12, 16, 7, 59, tzinfo=UTC))
        )

    def test_no_reservation_when_pacing_would_exceed_deadline(self):
        guard = self.guard()
        guard.before_request("a", 240)
        with self.assertRaises(QuotaWaitDeadline):
            guard.before_request("a", 10)
        self.assertEqual(1, guard.requests["a"])
        self.assertEqual([], self.clock.sleeps)

    def test_corrupt_or_missing_required_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            with self.assertRaises(QuotaStateError):
                QuotaGuard(ledger_path=str(path), required=True)
            path.write_text("broken JSON")
            with self.assertRaises(QuotaStateError):
                QuotaGuard(ledger_path=str(path))

    def test_invalid_counters_cannot_reset_daily_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            for count in (True, -1, "7"):
                path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "quota_day": "2026-10-15",
                            "requests": {"a": count},
                            "blocked_models": [],
                        }
                    )
                )
                with self.assertRaises(QuotaStateError):
                    self.guard(ledger_path=str(path))

    def test_provider_daily_block_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.json")
            guard = self.guard(ledger_path=path)
            guard.block_daily("a")
            restored = QuotaGuard(ledger_path=path, wall_clock=lambda: self.clock.wall)
            with self.assertRaises(QuotaStopped):
                restored.before_request("a", restored.now() + 240)

    def test_persist_failure_prevents_request(self):
        guard = self.guard(ledger_path="unused-ledger.json")
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(QuotaStateError):
                guard.before_request("a", 240)


class GuardedClientTests(unittest.TestCase):
    def client(self, *responses, limit=18, **kwargs):
        self.clock = Clock()
        self.session = FakeSession(*responses)
        self.guard = QuotaGuard(
            daily_limit=limit,
            now=self.clock.now,
            wall_clock=lambda: self.clock.wall,
            sleep=self.clock.sleep,
        )
        return GeminiClient(
            "test-key",
            session=self.session,
            quota_guard=self.guard,
            now=self.clock.now,
            sleep=self.clock.sleep,
            random_fn=lambda lo, hi: lo,
            **kwargs,
        )

    def call(self, client, **kwargs):
        kwargs.setdefault(
            "output_schema",
            {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}},
        )
        return client.request_json(
            model="a", system_instruction="role", payload={}, **kwargs
        )

    def test_retries_and_repair_are_reserved_and_paced(self):
        client = self.client(
            FakeResponse(503),
            gemini_response("invalid JSON"),
            gemini_response({"ok": True}),
        )
        self.assertEqual({"ok": True}, self.call(client))
        self.assertEqual(3, self.guard.requests["a"])
        self.assertEqual(30, self.clock.seconds)

    def test_daily_quota_stops_after_one_request_and_survives_other_nodes(self):
        logs = []
        client = self.client(quota_response(), logger=logs.append)
        for purpose in ("researcher", "writer"):
            with self.assertRaises(QuotaStopped):
                self.call(client, purpose=purpose)
        self.assertEqual(1, len(self.session.calls))
        self.assertEqual([], self.clock.sleeps)
        self.assertNotIn("secret-project-id", "\n".join(logs))

    def test_daily_quota_can_use_an_independent_fallback(self):
        client = self.client(
            quota_response(), gemini_response({"ok": True}), fallback_models=("b",)
        )
        self.assertEqual({"ok": True}, self.call(client))
        self.assertEqual({"a": 1, "b": 1}, self.guard.requests)
        self.assertEqual([], self.clock.sleeps)

    def test_minute_quota_waits_a_full_minute_without_downward_jitter(self):
        client = self.client(quota_response("PerMinute"), gemini_response({"ok": True}))
        self.call(client)
        self.assertEqual(60, self.clock.seconds)

    def test_retryinfo_is_honored_when_longer_than_the_minute_cooldown(self):
        client = self.client(
            quota_response("PerMinute", delay="70s"), gemini_response({"ok": True})
        )
        self.call(client)
        self.assertEqual([70.0], self.clock.sleeps)

    def test_budget_prevents_repair_from_sending_an_extra_request(self):
        client = self.client(gemini_response("invalid JSON"), limit=1)
        with self.assertRaises(QuotaStopped):
            self.call(client)
        self.assertEqual(1, len(self.session.calls))

    def test_exhausted_primary_budget_can_fall_back_without_an_extra_primary_post(self):
        client = self.client(
            gemini_response({"ok": True}),
            gemini_response({"ok": True}),
            limit=1,
            fallback_models=("b",),
        )
        self.call(client)
        self.call(client)
        self.assertEqual({"a": 1, "b": 1}, self.guard.requests)

    def test_real_server_failure_is_not_hidden_by_budget_exhaustion(self):
        client = self.client(FakeResponse(503), limit=1)
        with self.assertRaises(GeminiAPIError) as caught:
            self.call(client)
        self.assertEqual(503, caught.exception.status_code)
        self.assertEqual(1, len(self.session.calls))

    def test_retry_after_is_not_shortened_to_the_cap(self):
        client = self.client(FakeResponse(429, headers={"Retry-After": "300"}))
        with self.assertRaises(GeminiAPIError):
            self.call(client)
        self.assertEqual(1, len(self.session.calls))
        self.assertEqual([], self.clock.sleeps)

    def test_malformed_error_details_do_not_crash_or_expose_messages(self):
        self.assertEqual(
            ("unknown", None),
            quota_error_details(FakeResponse(429, {"error": {"details": "broken"}})),
        )

    def test_provider_daily_block_expires_at_reset_in_the_same_client(self):
        client = self.client(quota_response(), gemini_response({"ok": True}))
        with self.assertRaises(QuotaStopped):
            self.call(client)
        self.clock.wall += timedelta(days=1)
        self.assertEqual({"ok": True}, self.call(client))


class QuotaRunnerTests(unittest.TestCase):
    def run_graph(self, *results):
        self.alerts = []
        return run_daily(
            make_settings(),
            graph=FakeGraph(*results),
            candidates=candidates(3),
            alert_publisher=self.alerts.append,
            clock=lambda: 0,
        )

    def test_quota_stop_defers_items_without_failed_articles(self):
        result = self.run_graph(
            terminal(STATUS_SKIP), NodeExecutionError("writer", QuotaStopped("a"))
        )
        self.assertEqual("QUOTA_STOPPED", result["run_outcome"])
        self.assertEqual(0, result["failed"])
        self.assertEqual(2, result["deferred"])
        self.assertIn("Skipped: 1", self.alerts[0])
        self.assertIn("Quota stop: model=a", self.alerts[0])

    def test_quota_stop_keeps_real_failures_when_nothing_was_published(self):
        result = self.run_graph(
            NodeExecutionError(
                "writer", GeminiAPIError("unavailable", status_code=503, transient=True)
            ),
            NodeExecutionError("researcher", QuotaStopped("a")),
        )
        self.assertEqual("FAILED", result["run_outcome"])
        self.assertEqual(1, result["exit_code"])

    def test_quota_stop_after_publication_is_reported_explicitly(self):
        result = self.run_graph(
            terminal(STATUS_PUBLISHED), NodeExecutionError("writer", QuotaStopped("a"))
        )
        self.assertEqual("QUOTA_STOPPED", result["run_outcome"])
        self.assertEqual(1, result["published"])

    def test_missing_required_ledger_sends_failure_alert_without_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            alerts = []
            result = run_daily(
                make_settings(
                    gemini_quota_ledger_required=True,
                    gemini_quota_ledger_path=str(Path(directory) / "missing"),
                ),
                candidates=candidates(1),
                alert_publisher=alerts.append,
            )
            self.assertEqual("FAILED", result["run_outcome"])
            self.assertIn("Stage: quota", alerts[0])

    def test_alert_includes_skips_stages_and_all_http_error_counts(self):
        failures = [
            {"stage": "reviewer", "category": "TRANSIENT_RATE_LIMIT"},
            {"stage": "researcher", "category": "TRANSIENT_SERVER"},
        ]
        metrics = RunMetrics(
            gemini_requests=23,
            gemini_retries=11,
            http_statuses={200: 8, 429: 11, 503: 4},
        )
        message = "\n".join(
            _alert_lines(
                "部分成功",
                RunOutcome.PARTIAL_SUCCESS,
                {
                    "published": 1,
                    "selected": 6,
                    "processed": 6,
                    "failed": 2,
                    "skipped": 3,
                },
                failures,
                breaker_opened=False,
                fatal_failure=None,
                status_code=503,
                metrics=metrics,
            )
        )
        for expected in (
            "Skipped: 3",
            "HTTP errors: 429=11, 503=4",
            "researcher=1",
            "reviewer=1",
            "Last failed HTTP status: 503",
        ):
            self.assertIn(expected, message)


class QuotaConfigTests(unittest.TestCase):
    def test_quota_defaults_and_overrides(self):
        settings = Settings.from_env(env())
        self.assertEqual(15, settings.gemini_min_interval_seconds)
        self.assertEqual(18, settings.gemini_daily_request_limit)
        settings = Settings.from_env(
            env(
                GEMINI_MIN_INTERVAL_SECONDS="20",
                GEMINI_DAILY_REQUEST_LIMIT="10",
                GEMINI_QUOTA_LEDGER_PATH="ledger.json",
                GEMINI_QUOTA_LEDGER_REQUIRED="1",
            )
        )
        self.assertEqual(20, settings.gemini_min_interval_seconds)
        self.assertEqual(10, settings.gemini_daily_request_limit)
        self.assertTrue(settings.gemini_quota_ledger_required)

    def test_invalid_quota_limits_are_rejected(self):
        for overrides in (
            {"GEMINI_MIN_INTERVAL_SECONDS": "nan"},
            {"GEMINI_MIN_INTERVAL_SECONDS": "inf"},
            {"GEMINI_MIN_INTERVAL_SECONDS": "0"},
            {"GEMINI_DAILY_REQUEST_LIMIT": "0"},
        ):
            with self.assertRaises(ConfigError):
                Settings.from_env(env(**overrides))


class LedgerRestoreTests(unittest.TestCase):
    def audit(self, *, prior=None, document=None, extra_run=None, total_count=None):
        prior = prior or []
        artifact = {
            "id": 5,
            "name": "gemini-quota-ledger",
            "expired": False,
            "created_at": "2026-10-15T10:05:00Z",
            "workflow_run": {"id": 1},
        }
        archive = io.BytesIO()
        if document is not None:
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("gemini-quota-ledger.json", json.dumps(document))

        def get_json(path):
            if "/artifacts?" in path:
                return {"artifacts": [artifact] if document is not None else []}
            if "/workflows/" in path:
                return {
                    "workflow_runs": prior,
                    "total_count": len(prior) if total_count is None else total_count,
                }
            return {"workflow_id": 2, "run_attempt": 1}

        return restore_module.restore(
            get_json=get_json,
            get_bytes=lambda _: archive.getvalue(),
            repository="owner/repo",
            run_id=9,
            models=["a", "b"],
            limit=18,
            instant=datetime(2026, 10, 15, 12, tzinfo=UTC),
        )

    def prior(self, run_id=1, updated="2026-10-15T10:00:00Z"):
        return {"id": run_id, "updated_at": updated, "status": "completed"}

    def document(self, day="2026-10-15"):
        return {
            "version": 1,
            "quota_day": day,
            "requests": {"a": 7},
            "blocked_models": [],
        }

    def test_first_run_in_a_fresh_quota_day_starts_empty(self):
        self.assertEqual({}, self.audit()["requests"])

    def test_saved_attempts_are_restored(self):
        self.assertEqual(
            {"a": 7},
            self.audit(prior=[self.prior()], document=self.document())["requests"],
        )

    def test_legacy_or_unsaved_run_reserves_the_remaining_daily_budget(self):
        self.assertEqual(
            {"a": 18, "b": 18}, self.audit(prior=[self.prior()])["requests"]
        )

    def test_crash_after_last_saved_run_cannot_reset_the_budget(self):
        result = self.audit(
            prior=[self.prior(), self.prior(2, "2026-10-15T11:00:00Z")],
            document=self.document(),
        )
        self.assertEqual({"a": 18, "b": 18}, result["requests"])

    def test_corrupt_artifact_is_not_treated_as_a_fresh_day(self):
        with self.assertRaises(QuotaStateError):
            self.audit(prior=[self.prior()], document={"version": "broken"})

    def test_incomplete_run_inventory_fails_closed(self):
        with self.assertRaises(RuntimeError):
            self.audit(total_count=100)

    def test_unsaved_rerun_attempt_cannot_hide_behind_the_same_run_id(self):
        prior = self.prior()
        prior["run_attempt"] = 2
        result = self.audit(
            prior=[prior], document={**self.document(), "run_attempt": 1}
        )
        self.assertEqual({"a": 18, "b": 18}, result["requests"])

    def test_run_crossing_pacific_midnight_is_audited(self):
        self.assertEqual(
            {"a": 18, "b": 18},
            self.audit(prior=[self.prior(updated="2026-10-15T07:01:00Z")])["requests"],
        )

    def test_run_finished_before_pacific_reset_does_not_consume_today(self):
        self.assertEqual(
            {},
            self.audit(prior=[self.prior(updated="2026-10-15T06:59:00Z")])["requests"],
        )


if __name__ == "__main__":
    unittest.main()
