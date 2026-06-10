"""Claim an untracked IB position into the bot's positions.json.

Useful when option 4 detects a position in IB that the bot doesn't track
(manual orders, prior-bot orphans, paper-account residue). Claiming it
lets the bot manage stops + exits going forward.

The bot can't know the original entry price reliably, so this tool sets:
  entry_price = IB's avg cost (best available)
  stop = entry_price * (1 - DEFAULT_STOP_PCT)  (uses config)
  layer = 0 (treat as a pilot)
  peak = current market price (if higher than entry)

Usage:
  python scripts/claim_position.py --symbol JBL
  python scripts/claim_position.py --symbol JBL --stop-pct 0.06
  python scripts/claim_position.py --all-untracked    # claim every IB position
                                                       # not in positions.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Unique clientId per script invocation. PID-based: each subprocess gets
# a stable id derived from its PID. Avoids Error 326 collision with the
# running bot AND collisions between concurrent ad-hoc scripts (e.g.,
# multiple dashboard Sell clicks within seconds).
import os

os.environ["IBKR_CLIENT_ID"] = str(1000 + (os.getpid() % 9000))

from broker.ibkr_client import IBKRClient
from config import DEFAULT_STOP_PCT


def load_positions() -> dict:
    p = ROOT / "state" / "positions.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_positions(d: dict) -> None:
    p = ROOT / "state" / "positions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))


def claim(symbol: str, stop_pct: float, broker: IBKRClient) -> dict | None:
    # Find position in IB
    pos = next(
        (p for p in broker.ib.positions() if p.contract.symbol == symbol and int(p.position) != 0),
        None,
    )
    if pos is None:
        print(f"  {symbol}: no IB position — nothing to claim")
        return None

    shares = int(pos.position)
    avg_cost = float(pos.avgCost or 0)
    if avg_cost <= 0:
        print(f"  {symbol}: avg_cost is 0 — IB hasn't reported it; cannot claim safely")
        return None

    try:
        market = broker.market_price(symbol)
    except Exception as e:
        print(
            f"claim_position_market_price_failed: {symbol}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        market = avg_cost

    record = {
        "symbol": symbol,
        "shares": shares,
        "entry": round(avg_cost, 2),
        "stop": round(avg_cost * (1 - stop_pct), 2),
        "initial_stop": round(avg_cost * (1 - stop_pct), 2),
        "breakeven_moved": False,
        "peak": round(max(avg_cost, market), 2),
        "layer": 0,
        "layer_entries": [round(avg_cost, 2)],
        "entry_date": dt.date.today().isoformat(),
        "claimed": True,
        "claim_note": "untracked position claimed via claim_position.py — "
        "original entry timestamp unknown",
    }
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="claim one specific symbol")
    g.add_argument(
        "--all-untracked", action="store_true", help="claim every IB position not in positions.json"
    )
    ap.add_argument(
        "--stop-pct",
        type=float,
        default=DEFAULT_STOP_PCT,
        help=f"stop distance below entry (default {DEFAULT_STOP_PCT})",
    )
    args = ap.parse_args()

    broker = IBKRClient()
    broker.connect()
    try:
        positions = load_positions()

        if args.symbol:
            symbol = args.symbol.upper()
            if symbol in positions:
                print(f"{symbol} is already tracked — refusing to overwrite")
                return 1
            rec = claim(symbol, args.stop_pct, broker)
            if rec:
                positions[symbol] = rec
                save_positions(positions)
                print(f"\nclaimed {symbol}:")
                print(json.dumps(rec, indent=2))
            return 0 if rec else 1

        if args.all_untracked:
            ib_syms = {p.contract.symbol for p in broker.ib.positions() if int(p.position) != 0}
            untracked = ib_syms - set(positions.keys())
            if not untracked:
                print("no untracked positions in IB")
                return 0
            print(f"untracked positions found: {sorted(untracked)}")
            answer = input("claim them all? [y/N]: ").strip().lower()
            if answer != "y":
                print("aborted")
                return 0
            claimed = 0
            for sym in sorted(untracked):
                rec = claim(sym, args.stop_pct, broker)
                if rec:
                    positions[sym] = rec
                    claimed += 1
            save_positions(positions)
            print(f"\nclaimed {claimed} positions, written to state/positions.json")
            return 0
    finally:
        broker.disconnect()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
