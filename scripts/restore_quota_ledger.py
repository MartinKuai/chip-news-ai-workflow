"""Restore the latest Actions quota state, refusing to undercount missing runs.

Requires only the built-in, read-only Actions token. No Gemini/Telegram calls.
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from datetime import UTC, datetime, time
from pathlib import Path
from urllib.parse import urlencode
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from daily_chip_news.quota import PACIFIC, quota_day, validate_ledger

ARTIFACT_NAME = "gemini-quota-ledger"
LEDGER_NAME = "gemini-quota-ledger.json"


def restore(
    *,
    get_json,
    get_bytes,
    repository: str,
    run_id: int,
    models: list[str],
    limit: int,
    instant: datetime,
) -> dict:
    base = f"repos/{repository}/actions"
    current = get_json(f"{base}/runs/{run_id}")
    midnight = datetime.combine(instant.astimezone(PACIFIC).date(), time(), PACIFIC)
    # Old runs retain their original creation date when rerun today. Audit the
    # bounded full inventory by updated_at, including reruns and runs spanning reset.
    inventory = []
    for page in range(1, 11):
        query = urlencode({"per_page": 100, "page": page})
        response = get_json(f"{base}/workflows/{current['workflow_id']}/runs?{query}")
        inventory.extend(response["workflow_runs"])
        if response["total_count"] > 1000:
            raise RuntimeError("Too many runs to safely audit quota state")
        if len(inventory) >= response["total_count"]:
            break
        if len(response["workflow_runs"]) < 100:
            raise RuntimeError("Incomplete workflow run inventory")
    else:
        raise RuntimeError("Workflow run inventory exceeded the audit limit")
    midnight_utc = midnight.astimezone(UTC)
    prior = [
        run
        for run in inventory
        if (run["id"] != run_id or current.get("run_attempt", 1) > 1)
        and run["status"] not in {"queued", "waiting", "requested", "pending"}
        and datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
        >= midnight_utc
    ]
    artifacts = get_json(
        f"{base}/artifacts?" + urlencode({"per_page": 100, "name": ARTIFACT_NAME})
    )["artifacts"]
    candidates = [
        a
        for a in artifacts
        if a["name"] == ARTIFACT_NAME
        and not a["expired"]
        and a.get("workflow_run", {}).get("id") in {r["id"] for r in prior}
    ]
    latest = max(candidates, key=lambda a: a["created_at"], default=None)
    day = quota_day(instant)
    document = {"version": 1, "quota_day": day, "requests": {}, "blocked_models": []}
    if latest:
        archive = get_bytes(f"{base}/artifacts/{latest['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            info = zipped.getinfo(LEDGER_NAME)
            if info.file_size > 1_000_000:
                raise RuntimeError("Quota ledger artifact is too large")
            restored = validate_ledger(json.loads(zipped.read(info)))
        if restored["quota_day"] > day:
            raise RuntimeError("Quota ledger is dated in the future")
        if restored["quota_day"] == day:
            document = restored
        else:
            document["next_allowed_at"] = restored.get("next_allowed_at", {})
        # Any run whose final state was never saved may have consumed its full budget.
        missing = [
            run
            for run in prior
            if run["id"] != latest["workflow_run"]["id"]
            and run["updated_at"] > latest["created_at"]
        ]
        source_run = next(
            run for run in prior if run["id"] == latest["workflow_run"]["id"]
        )
        # The current attempt has not generated yet; preceding attempts have.
        completed_attempts = source_run.get("run_attempt", 1) - int(
            source_run["id"] == run_id
        )
        if completed_attempts > restored.get("run_attempt", 1):
            missing.append(source_run)
    else:
        missing = prior
    if missing:
        document["requests"].update(
            {model: max(limit, document["requests"].get(model, 0)) for model in models}
        )
        document["bootstrap_note"] = (
            "Unaccounted earlier run: reserve the remaining daily budget"
        )
    document["run_id"] = run_id
    document["run_attempt"] = current.get("run_attempt", 1)
    print(
        f"Quota ledger restored | day={day} | audited_runs={len(prior)} | unaccounted_runs={len(missing)}"
    )
    return validate_ledger(document)


def main() -> None:
    token = os.environ["GH_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]

    def fetch(path: str) -> bytes:
        response = requests.get(
            f"https://api.github.com/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        # requests strips Authorization when the artifact redirect changes host.
        response.raise_for_status()
        return response.content

    models = list(
        dict.fromkeys(
            filter(
                None,
                [
                    os.environ.get(name, "").strip()
                    for name in ("RESEARCHER_MODEL", "WRITER_MODEL", "REVIEWER_MODEL")
                ]
                + [
                    item.strip()
                    for item in os.environ.get("GEMINI_FALLBACK_MODELS", "").split(",")
                ],
            )
        )
    )
    document = restore(
        get_json=lambda path: json.loads(fetch(path)),
        get_bytes=fetch,
        repository=repository,
        run_id=int(os.environ["GITHUB_RUN_ID"]),
        models=models,
        limit=int(os.environ.get("GEMINI_DAILY_REQUEST_LIMIT", "18")),
        instant=datetime.now(UTC),
    )
    Path(os.environ["GEMINI_QUOTA_LEDGER_PATH"]).write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print request headers, redirects, response bodies or credentials.
        print(
            f"Quota ledger restore failed | error={type(exc).__name__}", file=sys.stderr
        )
        raise SystemExit(1) from None
