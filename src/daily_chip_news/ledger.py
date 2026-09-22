"""Rolling 30-day cost ledger persisted through GitHub Actions artifacts.

The ledger is append-only JSONL: one logical entry per billable call, keyed by
``entry_id``. Later records with the same id resolve earlier ones (reservation →
consumed / unresolved / released), so a crashed run can never undercount its
own spend: an unresolved reservation still counts at its projected cost.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .cost import UsageTotals
from .cost_guard import Reservation

LEDGER_ARTIFACT_NAME = "gemini-cost-ledger"
ROLLING_WINDOW_DAYS = 30
RETENTION_DAYS = 45

LEDGER_INIT = "ledger_init"
RUN_OPEN = "run_open"
RESERVATION = "reservation"
CALL = "call"
ANCHOR = "anchor"


class LedgerError(RuntimeError):
    """Base class for ledger failures that must stop paid calls."""


class LedgerCorruptError(LedgerError):
    """The stored ledger exists but cannot be trusted."""


class LedgerReadError(LedgerError):
    """The stored ledger could not be retrieved from artifact storage."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise LedgerCorruptError("ledger entry has no timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LedgerCorruptError(f"ledger timestamp is invalid: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class LedgerEntry:
    """One ledger record; later records may resolve earlier ones by entry_id."""

    kind: str
    entry_id: str
    ts: datetime
    state: str = ""
    run_id: str = ""
    mode: str = ""
    model: str = ""
    purpose: str = ""
    prompt_tokens: int = 0
    candidate_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    billable_requests: int = 0
    projected_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    note: str = ""

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": self.kind,
            "entry_id": self.entry_id,
            "ts": self.ts.isoformat(),
        }
        for name in (
            "state",
            "run_id",
            "mode",
            "model",
            "purpose",
            "prompt_tokens",
            "candidate_tokens",
            "thought_tokens",
            "total_tokens",
            "billable_requests",
            "projected_cost_usd",
            "actual_cost_usd",
            "note",
        ):
            value = getattr(self, name)
            if value not in ("", 0, 0.0, None):
                record[name] = value
        return record

    @classmethod
    def from_record(cls, record: Any) -> LedgerEntry:
        if not isinstance(record, dict):
            raise LedgerCorruptError("ledger line is not a JSON object")
        kind = record.get("kind")
        entry_id = record.get("entry_id")
        if not isinstance(kind, str) or not kind:
            raise LedgerCorruptError("ledger line has no kind")
        if not isinstance(entry_id, str) or not entry_id:
            raise LedgerCorruptError("ledger line has no entry_id")
        try:
            return cls(
                kind=kind,
                entry_id=entry_id,
                ts=_parse_ts(record.get("ts")),
                state=str(record.get("state", "")),
                run_id=str(record.get("run_id", "")),
                mode=str(record.get("mode", "")),
                model=str(record.get("model", "")),
                purpose=str(record.get("purpose", "")),
                prompt_tokens=int(record.get("prompt_tokens", 0) or 0),
                candidate_tokens=int(record.get("candidate_tokens", 0) or 0),
                thought_tokens=int(record.get("thought_tokens", 0) or 0),
                total_tokens=int(record.get("total_tokens", 0) or 0),
                billable_requests=int(record.get("billable_requests", 0) or 0),
                projected_cost_usd=float(record.get("projected_cost_usd", 0.0) or 0.0),
                actual_cost_usd=(
                    None
                    if record.get("actual_cost_usd") is None
                    else float(record["actual_cost_usd"])
                ),
                note=str(record.get("note", "")),
            )
        except (TypeError, ValueError) as exc:
            raise LedgerCorruptError(f"ledger line has invalid fields: {exc}") from exc


@dataclass
class CostLedger:
    """Append-only ledger with rolling-window spend accounting."""

    entries: list[LedgerEntry] = field(default_factory=list)

    # --- construction -----------------------------------------------------
    @classmethod
    def empty(cls, *, now: Callable[[], datetime] | None = None) -> CostLedger:
        clock = now or _utc_now
        return cls(
            entries=[
                LedgerEntry(
                    kind=LEDGER_INIT,
                    entry_id=f"init:{clock().isoformat()}",
                    ts=clock(),
                    note="ledger initialized",
                )
            ]
        )

    @classmethod
    def from_jsonl(cls, text: str) -> CostLedger:
        entries: list[LedgerEntry] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError as exc:
                raise LedgerCorruptError(
                    f"ledger line {line_number} is not valid JSON"
                ) from exc
            entries.append(LedgerEntry.from_record(record))
        if not entries:
            raise LedgerCorruptError("ledger file is empty")
        return cls(entries=entries)

    @classmethod
    def load(cls, path: Path) -> CostLedger:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerReadError(f"cannot read ledger at {path}") from exc
        return cls.from_jsonl(text)

    # --- persistence ------------------------------------------------------
    def to_jsonl(self, *, now: datetime | None = None) -> str:
        cutoff = (now or _utc_now()) - timedelta(days=RETENTION_DAYS)
        lines = [
            json.dumps(entry.to_record(), ensure_ascii=False, separators=(",", ":"))
            for entry in self.entries
            if entry.ts >= cutoff
        ]
        return "\n".join(lines) + "\n"

    def save(self, path: Path, *, now: datetime | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(now=now), encoding="utf-8")

    # --- accounting -------------------------------------------------------
    def append(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def latest_by_id(self) -> dict[str, LedgerEntry]:
        latest: dict[str, LedgerEntry] = {}
        for entry in self.entries:
            current = latest.get(entry.entry_id)
            if current is None or entry.ts >= current.ts:
                latest[entry.entry_id] = entry
        return latest

    def rolling_spend_usd(
        self,
        *,
        now: datetime | None = None,
        window_days: int = ROLLING_WINDOW_DAYS,
    ) -> float:
        cutoff = (now or _utc_now()) - timedelta(days=window_days)
        total = 0.0
        for entry in self.latest_by_id().values():
            if entry.ts < cutoff:
                continue
            if entry.state == "consumed":
                total += entry.actual_cost_usd or 0.0
            elif entry.state in {"reserved", "unresolved"}:
                total += entry.projected_cost_usd
        return total

    def run_spend_usd(self, run_id: str) -> float:
        total = 0.0
        for entry in self.latest_by_id().values():
            if entry.run_id != run_id:
                continue
            if entry.state == "consumed":
                total += entry.actual_cost_usd or 0.0
            elif entry.state in {"reserved", "unresolved"}:
                total += entry.projected_cost_usd
        return total

    # --- lifecycle helpers ------------------------------------------------
    def entry_from_reservation(
        self,
        reservation: Reservation,
        *,
        run_id: str,
        mode: str,
        state: str,
    ) -> LedgerEntry:
        return LedgerEntry(
            kind=RESERVATION,
            entry_id=reservation.reservation_id,
            ts=_utc_now(),
            state=state,
            run_id=run_id,
            mode=mode,
            model=reservation.model,
            purpose=reservation.purpose,
            projected_cost_usd=reservation.projected_cost_usd,
        )

    def append_run_open(
        self,
        *,
        run_id: str,
        projected_cost_usd: float,
        mode: str,
        now: Callable[[], datetime] | None = None,
    ) -> LedgerEntry:
        clock = now or _utc_now
        entry = LedgerEntry(
            kind=RUN_OPEN,
            entry_id=f"run:{run_id}",
            ts=clock(),
            state="reserved",
            run_id=run_id,
            mode=mode,
            projected_cost_usd=projected_cost_usd,
            note="run budget held until the run finalizes",
        )
        self.append(entry)
        return entry

    def resolve_run_open(
        self,
        *,
        run_id: str,
        actual_cost_usd: float,
        now: Callable[[], datetime] | None = None,
    ) -> LedgerEntry | None:
        """Resolve the prepared run reservation down to the actual run spend."""
        entry_id = f"run:{run_id}"
        if not any(entry.entry_id == entry_id for entry in self.entries):
            # Local runs without the prepare step have no run reservation; do
            # not fabricate one, or the spend would be counted twice.
            return None
        clock = now or _utc_now
        entry = LedgerEntry(
            kind=RUN_OPEN,
            entry_id=entry_id,
            ts=clock(),
            state="consumed",
            run_id=run_id,
            projected_cost_usd=0.0,
            actual_cost_usd=actual_cost_usd,
            note="run finalized",
        )
        self.append(entry)
        return entry

    def append_anchor(
        self,
        *,
        amount_usd: float,
        note: str,
        now: Callable[[], datetime] | None = None,
    ) -> LedgerEntry:
        """Manual re-anchor after a lost artifact; never silently resets to zero."""
        clock = now or _utc_now
        entry = LedgerEntry(
            kind=ANCHOR,
            entry_id=f"anchor:{clock().isoformat()}",
            ts=clock(),
            state="consumed",
            actual_cost_usd=max(0.0, float(amount_usd)),
            note=note or "manual anchor",
        )
        self.append(entry)
        return entry

    def record_guard_event(
        self,
        event: str,
        reservation: Reservation,
        usage: UsageTotals | None,
        *,
        run_id: str,
        mode: str,
    ) -> None:
        """Recorder callback used by :class:`CostGuard` to persist every event."""
        state = reservation.state
        entry = LedgerEntry(
            kind=CALL if event == "reconcile" and usage is not None else RESERVATION,
            entry_id=reservation.reservation_id,
            ts=_utc_now(),
            state=state,
            run_id=run_id,
            mode=mode,
            model=reservation.model,
            purpose=reservation.purpose,
            projected_cost_usd=reservation.projected_cost_usd,
            actual_cost_usd=usage.cost_usd if usage is not None else None,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            candidate_tokens=usage.candidate_tokens if usage else 0,
            thought_tokens=usage.thought_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            billable_requests=usage.billable_requests if usage else 0,
        )
        self.append(entry)


def fetch_previous_ledger(
    *,
    repo: str,
    token: str,
    run_id: str,
    session: requests.Session | None = None,
    api_url: str = "https://api.github.com",
    artifact_name: str = LEDGER_ARTIFACT_NAME,
) -> str | None:
    """Return the newest ledger artifact text from a previous run, or None.

    Raises :class:`LedgerReadError` when artifact storage cannot be read, so the
    caller can fail closed instead of starting a fresh ledger.
    """
    if not repo or not token:
        return None
    http = session or requests.Session()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    listing_url = (
        f"{api_url}/repos/{repo}/actions/artifacts"
        f"?name={artifact_name}&per_page=30"
    )
    try:
        listing = http.get(listing_url, headers=headers, timeout=30.0)
    except requests.RequestException as exc:
        raise LedgerReadError("cannot list ledger artifacts") from exc
    if listing.status_code != 200:
        raise LedgerReadError(
            f"cannot list ledger artifacts (HTTP {listing.status_code})"
        )
    try:
        artifacts = listing.json().get("artifacts", [])
    except (ValueError, AttributeError) as exc:
        raise LedgerReadError("artifact listing was unreadable") from exc

    candidates = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("expired"):
            continue
        workflow_run = artifact.get("workflow_run") or {}
        if str(workflow_run.get("id", "")) == str(run_id):
            continue
        if not artifact.get("archive_download_url"):
            continue
        candidates.append(artifact)
    if not candidates:
        return None
    newest = max(candidates, key=lambda item: str(item.get("created_at", "")))
    try:
        archive = http.get(
            newest["archive_download_url"], headers=headers, timeout=60.0
        )
    except requests.RequestException as exc:
        raise LedgerReadError("cannot download the ledger artifact") from exc
    if archive.status_code != 200:
        raise LedgerReadError(
            f"cannot download the ledger artifact (HTTP {archive.status_code})"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            names = [
                name for name in bundle.namelist() if name.endswith("ledger.jsonl")
            ]
            if not names:
                raise LedgerReadError("ledger artifact has no ledger.jsonl")
            return bundle.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise LedgerReadError("ledger artifact is not a valid zip") from exc


def latest_entries(entries: Iterable[LedgerEntry]) -> list[LedgerEntry]:
    """Utility for reporting the resolved view of an entry stream."""
    latest: dict[str, LedgerEntry] = {}
    for entry in entries:
        current = latest.get(entry.entry_id)
        if current is None or entry.ts >= current.ts:
            latest[entry.entry_id] = entry
    return list(latest.values())
