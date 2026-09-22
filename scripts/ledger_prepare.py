"""Prepare the rolling cost ledger before any paid Gemini call.

Reads the newest ``gemini-cost-ledger`` artifact from a previous run, fails
closed when it cannot be trusted, records this run's open budget reservation
and writes the ledger that ``main.py`` will append to.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from daily_chip_news.config import ConfigError, budget_settings_from_env  # noqa: E402
from daily_chip_news.ledger import (  # noqa: E402
    CostLedger,
    LedgerCorruptError,
    LedgerReadError,
    fetch_previous_ledger,
)


def main() -> None:
    env = os.environ
    ledger_path = Path(env.get("COST_LEDGER_PATH", ".cost/ledger.jsonl"))
    run_id = env.get("GITHUB_RUN_ID", "local")
    repo = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GITHUB_TOKEN", "")
    anchor = env.get("LEDGER_ANCHOR_USD", "").strip()
    anchor_note = env.get("LEDGER_ANCHOR_NOTE", "").strip()

    try:
        mode, scheduled_budget, manual_budget = budget_settings_from_env(env)
    except ConfigError as exc:
        print(f"Cost configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    run_budget = scheduled_budget if mode == "scheduled" else manual_budget

    previous = None
    if repo and token:
        try:
            previous = fetch_previous_ledger(repo=repo, token=token, run_id=run_id)
        except LedgerReadError as exc:
            print(
                f"Cost ledger read failed: {exc} | action=fail_closed",
                file=sys.stderr,
            )
            raise SystemExit(3)

    if previous:
        try:
            ledger = CostLedger.from_jsonl(previous)
        except LedgerCorruptError as exc:
            print(
                f"Cost ledger corrupt: {exc} | action=fail_closed",
                file=sys.stderr,
            )
            raise SystemExit(3)
        print(
            "Cost ledger loaded | "
            f"entries={len(ledger.entries)} "
            f"| rolling_30d_spend_usd={ledger.rolling_spend_usd():.6f}"
        )
    else:
        ledger = CostLedger.empty()
        when = "no previous artifact found" if repo and token else "local run"
        print(f"Cost ledger initialized | reason={when}")

    if anchor:
        try:
            anchor_value = float(anchor)
        except ValueError:
            print(
                f"LEDGER_ANCHOR_USD is not a number: {anchor!r}",
                file=sys.stderr,
            )
            raise SystemExit(4)
        ledger.append_anchor(
            amount_usd=anchor_value,
            note=anchor_note or "manual re-anchor",
        )
        print(f"Cost ledger anchored | amount_usd={anchor_value:.6f}")

    ledger.append_run_open(
        run_id=run_id,
        projected_cost_usd=run_budget,
        mode=mode,
    )
    ledger.save(ledger_path)
    print(
        "Run open reservation | "
        f"run_id={run_id} | mode={mode} | run_budget_usd={run_budget:.4f} "
        f"| rolling_30d_spend_usd={ledger.rolling_spend_usd():.6f} "
        f"| ledger={ledger_path}"
    )


if __name__ == "__main__":
    main()
