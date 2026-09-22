from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from _support import SRC  # noqa: F401
from daily_chip_news.cost import UsageTotals
from daily_chip_news.cost_guard import CostGuard, Reservation
from daily_chip_news.ledger import (
    CostLedger,
    LedgerCorruptError,
    LedgerEntry,
    LedgerReadError,
    fetch_previous_ledger,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def ts(days_ago: float = 0.0) -> datetime:
    return NOW - timedelta(days=days_ago)


def reservation(reservation_id: str = "res-1", projected: float = 0.02) -> Reservation:
    return Reservation(
        reservation_id=reservation_id,
        model="gemini-3.5-flash-lite",
        purpose="researcher",
        projected_cost_usd=projected,
    )


class LedgerFormatTests(unittest.TestCase):
    def test_round_trip_preserves_entries(self) -> None:
        ledger = CostLedger.empty(now=lambda: NOW)
        ledger.append_run_open(
            run_id="42", projected_cost_usd=0.20, mode="scheduled", now=lambda: NOW
        )
        text = ledger.to_jsonl(now=NOW)
        restored = CostLedger.from_jsonl(text)
        self.assertEqual(len(ledger.entries), len(restored.entries))
        self.assertEqual("run:42", restored.entries[-1].entry_id)

    def test_save_and_load_from_disk(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "ledger.jsonl"
            ledger = CostLedger.empty(now=lambda: NOW)
            ledger.save(path, now=NOW)
            restored = CostLedger.load(path)
            self.assertEqual(1, len(restored.entries))

    def test_corrupt_lines_fail_closed(self) -> None:
        with self.assertRaises(LedgerCorruptError):
            CostLedger.from_jsonl("not json")
        with self.assertRaises(LedgerCorruptError):
            CostLedger.from_jsonl(json.dumps({"kind": "call"}))
        with self.assertRaises(LedgerCorruptError):
            CostLedger.from_jsonl(
                json.dumps({"kind": "call", "entry_id": "x", "ts": "yesterday"})
            )
        with self.assertRaises(LedgerCorruptError):
            CostLedger.from_jsonl("")

    def test_entries_older_than_retention_are_pruned(self) -> None:
        ledger = CostLedger.empty(now=lambda: ts(60))
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="old",
                ts=ts(50),
                state="consumed",
                actual_cost_usd=1.0,
            )
        )
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="fresh",
                ts=ts(1),
                state="consumed",
                actual_cost_usd=0.5,
            )
        )
        text = ledger.to_jsonl(now=NOW)
        self.assertNotIn('"old"', text)
        self.assertIn('"fresh"', text)


class RollingSpendTests(unittest.TestCase):
    def make_ledger(self) -> CostLedger:
        ledger = CostLedger.empty(now=lambda: NOW)
        ledger.append_run_open(
            run_id="42", projected_cost_usd=0.20, mode="scheduled", now=lambda: NOW
        )
        return ledger

    def test_run_open_reservation_counts_until_resolved(self) -> None:
        ledger = self.make_ledger()
        self.assertAlmostEqual(0.20, ledger.rolling_spend_usd(now=NOW), places=9)
        ledger.resolve_run_open(
            run_id="42", actual_cost_usd=0.03, now=lambda: NOW
        )
        self.assertAlmostEqual(0.03, ledger.rolling_spend_usd(now=NOW), places=9)

    def test_consumed_and_unresolved_count_but_released_does_not(self) -> None:
        ledger = self.make_ledger()
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="consumed",
                ts=ts(),
                state="consumed",
                actual_cost_usd=0.01,
            )
        )
        ledger.append(
            LedgerEntry(
                kind="reservation",
                entry_id="unresolved",
                ts=ts(),
                state="unresolved",
                projected_cost_usd=0.02,
            )
        )
        ledger.append(
            LedgerEntry(
                kind="reservation",
                entry_id="released",
                ts=ts(),
                state="released",
                projected_cost_usd=0.05,
            )
        )
        self.assertAlmostEqual(
            0.20 + 0.01 + 0.02, ledger.rolling_spend_usd(now=NOW), places=9
        )

    def test_later_records_resolve_earlier_ones(self) -> None:
        ledger = CostLedger.empty(now=lambda: NOW)
        ledger.append(
            LedgerEntry(
                kind="reservation",
                entry_id="res-1",
                ts=ts(0.01),
                state="reserved",
                projected_cost_usd=0.02,
            )
        )
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="res-1",
                ts=ts(),
                state="consumed",
                actual_cost_usd=0.004,
            )
        )
        self.assertAlmostEqual(0.004, ledger.rolling_spend_usd(now=NOW), places=9)

    def test_entries_outside_the_window_are_ignored(self) -> None:
        ledger = CostLedger.empty(now=lambda: NOW)
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="old",
                ts=ts(31),
                state="consumed",
                actual_cost_usd=1.0,
            )
        )
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="recent",
                ts=ts(29),
                state="consumed",
                actual_cost_usd=0.5,
            )
        )
        self.assertAlmostEqual(0.5, ledger.rolling_spend_usd(now=NOW), places=9)

    def test_run_spend_is_scoped_to_one_run(self) -> None:
        ledger = CostLedger.empty(now=lambda: NOW)
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="a",
                ts=ts(),
                state="consumed",
                run_id="42",
                actual_cost_usd=0.01,
            )
        )
        ledger.append(
            LedgerEntry(
                kind="call",
                entry_id="b",
                ts=ts(),
                state="consumed",
                run_id="43",
                actual_cost_usd=0.02,
            )
        )
        self.assertAlmostEqual(0.01, ledger.run_spend_usd("42"), places=9)

    def test_anchor_adds_recovered_spend(self) -> None:
        ledger = CostLedger.empty(now=lambda: NOW)
        ledger.append_anchor(amount_usd=1.25, note="lost artifact", now=lambda: NOW)
        self.assertAlmostEqual(1.25, ledger.rolling_spend_usd(now=NOW), places=9)


class GuardRecorderTests(unittest.TestCase):
    def test_guard_events_land_in_the_ledger_and_stop_overcounting(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            ledger = CostLedger.empty(now=lambda: NOW)
            ledger.save(path, now=NOW)

            guard = CostGuard(
                run_budget_usd=0.20,
                rolling_budget_usd=7.50,
                recorder=lambda event, res, usage: (
                    ledger.record_guard_event(
                        event, res, usage, run_id="42", mode="scheduled"
                    ),
                    ledger.save(path),
                ),
            )
            reservation_handle = guard.authorize(
                model="gemini-3.5-flash-lite",
                purpose="researcher",
                prompt_tokens=4_000,
                max_output_tokens=3_072,
            )
            projected = reservation_handle.projected_cost_usd
            self.assertAlmostEqual(projected, ledger.rolling_spend_usd(now=NOW), places=9)

            guard.reconcile(
                reservation_handle,
                UsageTotals(
                    prompt_tokens=4_000,
                    candidate_tokens=100,
                    thought_tokens=50,
                    total_tokens=4_150,
                    billable_requests=1,
                    cost_usd=0.001575,
                ),
            )
            self.assertAlmostEqual(
                0.001575, ledger.rolling_spend_usd(now=NOW), places=9
            )
            self.assertAlmostEqual(0.001575, ledger.run_spend_usd("42"), places=9)
            restored = CostLedger.load(path)
            self.assertEqual(len(ledger.entries), len(restored.entries))


class FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


def zip_with_ledger(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("ledger.jsonl", text)
    return buffer.getvalue()


def artifact(artifact_id: int, created_at: str, run_id: int, *, expired: bool = False):
    return {
        "id": artifact_id,
        "created_at": created_at,
        "expired": expired,
        "archive_download_url": f"https://api.github.com/artifacts/{artifact_id}/zip",
        "workflow_run": {"id": run_id},
    }


class FetchPreviousLedgerTests(unittest.TestCase):
    def test_returns_the_newest_artifact_from_another_run(self) -> None:
        ledger_text = CostLedger.empty(now=lambda: NOW).to_jsonl(now=NOW)
        http = FakeHttp(
            [
                FakeResponse(
                    200,
                    {
                        "artifacts": [
                            artifact(1, "2026-09-19T00:00:00Z", 11),
                            artifact(2, "2026-09-20T00:00:00Z", 12),
                            artifact(3, "2026-09-21T00:00:00Z", 99),  # current run
                        ]
                    },
                ),
                FakeResponse(200, content=zip_with_ledger(ledger_text)),
            ]
        )
        text = fetch_previous_ledger(
            repo="owner/repo", token="t", run_id="99", session=http
        )
        self.assertEqual(ledger_text, text)
        self.assertIn("artifacts/2/zip", http.calls[1])

    def test_expired_artifacts_are_skipped(self) -> None:
        http = FakeHttp(
            [
                FakeResponse(
                    200,
                    {
                        "artifacts": [
                            artifact(1, "2026-09-20T00:00:00Z", 12, expired=True)
                        ]
                    },
                )
            ]
        )
        self.assertIsNone(
            fetch_previous_ledger(
                repo="owner/repo", token="t", run_id="99", session=http
            )
        )
        self.assertEqual(1, len(http.calls))

    def test_no_artifacts_returns_none(self) -> None:
        http = FakeHttp([FakeResponse(200, {"artifacts": []})])
        self.assertIsNone(
            fetch_previous_ledger(
                repo="owner/repo", token="t", run_id="99", session=http
            )
        )

    def test_missing_credentials_return_none(self) -> None:
        self.assertIsNone(fetch_previous_ledger(repo="", token="", run_id="99"))

    def test_listing_failure_fails_closed(self) -> None:
        http = FakeHttp([FakeResponse(500, {})])
        with self.assertRaises(LedgerReadError):
            fetch_previous_ledger(
                repo="owner/repo", token="t", run_id="99", session=http
            )

    def test_download_failure_fails_closed(self) -> None:
        http = FakeHttp(
            [
                FakeResponse(
                    200, {"artifacts": [artifact(1, "2026-09-20T00:00:00Z", 12)]}
                ),
                FakeResponse(403, {}),
            ]
        )
        with self.assertRaises(LedgerReadError):
            fetch_previous_ledger(
                repo="owner/repo", token="t", run_id="99", session=http
            )

    def test_artifact_without_ledger_file_fails_closed(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("other.txt", "hello")
        http = FakeHttp(
            [
                FakeResponse(
                    200, {"artifacts": [artifact(1, "2026-09-20T00:00:00Z", 12)]}
                ),
                FakeResponse(200, content=buffer.getvalue()),
            ]
        )
        with self.assertRaises(LedgerReadError):
            fetch_previous_ledger(
                repo="owner/repo", token="t", run_id="99", session=http
            )

    def test_invalid_zip_fails_closed(self) -> None:
        http = FakeHttp(
            [
                FakeResponse(
                    200, {"artifacts": [artifact(1, "2026-09-20T00:00:00Z", 12)]}
                ),
                FakeResponse(200, content=b"not a zip"),
            ]
        )
        with self.assertRaises(LedgerReadError):
            fetch_previous_ledger(
                repo="owner/repo", token="t", run_id="99", session=http
            )


if __name__ == "__main__":
    unittest.main()
