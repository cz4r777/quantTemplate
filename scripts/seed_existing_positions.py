"""Seed pre-existing operator-held option positions into positions.json.

T-BOT-LIVE-ENABLEMENT1.

The live account may already hold option contracts the bot has no state
for. Two acceptable handling paths:
  1. operator unwinds them by hand at IB
  2. seed them into positions.json with manage=False so the bot is aware
     of the symbols (dashboard shows P&L, manage_existing skips them,
     consider_new_entries refuses to open a new entry on the same
     symbol because it's already in positions)

This script implements (2). It NEVER places a broker order. It only
writes local state.

Input file shape (JSON array):

    [
      {
        "symbol":   "AAPL",
        "right":    "C",
        "strike":   340.0,
        "expiry":   "20260717",
        "contracts": 20,
        "avg_cost_per_contract": 119.4183,
        "currency": "USD"
      },
      ...
    ]

`avg_cost_per_contract` is IB's avgCost field for the position (premium
per share × 100 — IB's per-contract figure). The seeder divides by 100
to write a per-share `premium_entry` consistent with the rest of the
bot.

Usage:

    python scripts/seed_existing_positions.py --emit-template
    python scripts/seed_existing_positions.py --input seed.json --dry-run
    python scripts/seed_existing_positions.py --input seed.json --yes

Operator-side environment (Kali, NOT git-tracked) for the live cutover:

    IBKR_PORT=7496
    IBKR_MODE=live
    IBKR_ACCOUNT_ID=YOUR_ACCOUNT_ID

The bot's existing live-launcher gates (LIVE_LAUNCHER_ONE_CYCLE marker
+ identity-bearing confirm phrase) still apply regardless of .env.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

POSITIONS_PATH = ROOT / "state" / "positions.json"

TEMPLATE_AAPL_GOOG_NVDA: list[dict] = [
    {
        "symbol": "AAPL",
        "right": "C",
        "strike": 340.0,
        "expiry": "20260717",
        "contracts": 20,
        "avg_cost_per_contract": 119.4183,
        "currency": "USD",
    },
    {
        "symbol": "GOOG",
        "right": "C",
        "strike": 435.0,
        "expiry": "20260717",
        "contracts": 10,
        "avg_cost_per_contract": 830.4483,
        "currency": "USD",
    },
    {
        "symbol": "NVDA",
        "right": "C",
        "strike": 260.0,
        "expiry": "20260717",
        "contracts": 8,
        "avg_cost_per_contract": 690.2782,
        "currency": "USD",
    },
]

REQUIRED_KEYS = (
    "symbol",
    "right",
    "strike",
    "expiry",
    "contracts",
    "avg_cost_per_contract",
)


def _validate(row: dict) -> str | None:
    for k in REQUIRED_KEYS:
        if k not in row:
            return f"missing key: {k}"
    if row["right"] not in ("C", "P"):
        return f"right must be C or P, got {row['right']!r}"
    if not isinstance(row["expiry"], str) or len(row["expiry"]) != 8:
        return f"expiry must be YYYYMMDD string, got {row['expiry']!r}"
    if float(row["strike"]) <= 0:
        return f"strike must be positive, got {row['strike']!r}"
    if int(row["contracts"]) <= 0:
        return f"contracts must be positive, got {row['contracts']!r}"
    if float(row["avg_cost_per_contract"]) <= 0:
        return f"avg_cost_per_contract must be positive, got {row['avg_cost_per_contract']!r}"
    return None


def _load_positions() -> dict:
    if not POSITIONS_PATH.exists():
        return {}
    try:
        return json.loads(POSITIONS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_positions(positions: dict) -> None:
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_PATH.write_text(json.dumps(positions, indent=2))


def build_row(seed: dict) -> dict:
    """Convert one input-spec row to the on-disk positions.json shape."""
    per_share = float(seed["avg_cost_per_contract"]) / 100.0
    return {
        "contracts": int(seed["contracts"]),
        "premium_entry": round(per_share, 4),
        "strike": float(seed["strike"]),
        "expiry": seed["expiry"],
        "right": seed["right"],
        "currency": seed.get("currency", "USD"),
        "entry_date": dt.date.today().isoformat(),
        "claimed": True,
        "manage": False,
        "claim_note": (
            "operator-held position seeded via "
            "seed_existing_positions.py — bot manages NO entries, "
            "exits, trims, or exercises for this row"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="JSON file with the seed rows")
    g.add_argument(
        "--emit-template",
        action="store_true",
        help="print the example seed JSON to stdout and exit",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would change; do not write state"
    )
    ap.add_argument(
        "--yes", action="store_true", help="skip interactive confirm (cron / automation)"
    )
    args = ap.parse_args()

    if args.emit_template:
        print(json.dumps(TEMPLATE_AAPL_GOOG_NVDA, indent=2))
        return 0

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"REFUSED — input file not found: {input_path}", file=sys.stderr)
        return 2

    try:
        seeds = json.loads(input_path.read_text())
    except json.JSONDecodeError as e:
        print(f"REFUSED — input is not valid JSON: {e}", file=sys.stderr)
        return 2

    if not isinstance(seeds, list) or not seeds:
        print("REFUSED — input must be a non-empty JSON array", file=sys.stderr)
        return 2

    for i, row in enumerate(seeds):
        err = _validate(row)
        if err is not None:
            print(f"REFUSED — row {i}: {err}", file=sys.stderr)
            return 2

    positions = _load_positions()
    plan: list[tuple[str, dict]] = []
    refused: list[tuple[str, str]] = []
    for row in seeds:
        sym = str(row["symbol"]).upper()
        if sym in positions:
            refused.append((sym, "already tracked in positions.json"))
            continue
        plan.append((sym, build_row(row)))

    print(f"seed plan: {len(plan)} rows to write, {len(refused)} refused")
    for sym, record in plan:
        print(
            f"  + {sym}  {record['right']} ${record['strike']} "
            f"{record['expiry']} x{record['contracts']}  "
            f"premium_entry=${record['premium_entry']}  "
            f"manage={record['manage']}"
        )
    for sym, reason in refused:
        print(f"  - {sym}  REFUSED: {reason}")

    if args.dry_run:
        print("dry-run — no state written")
        return 0
    if not plan:
        print("nothing to write")
        return 0

    if not args.yes:
        answer = input("proceed with seed? [y/N]: ").strip().lower()
        if answer != "y":
            print("aborted")
            return 0

    for sym, record in plan:
        positions[sym] = record
    _save_positions(positions)
    print(f"wrote {len(plan)} rows to {POSITIONS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
