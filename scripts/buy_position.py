"""Manual stock BUY via IB.

Adds N shares of a symbol to the IB account. Optionally adds to bot tracking
in positions.json so the bot manages it from here.

Usage:
  python scripts/buy_position.py --symbol AAPL --shares 10
  python scripts/buy_position.py --symbol AAPL --shares 10 --no-track
  python scripts/buy_position.py --symbol AAPL --shares 10 --force   # skip confirm
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
from execution import exec_log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--shares", type=int, required=True)
    ap.add_argument(
        "--no-track", action="store_true", help="don't add to positions.json (bot won't manage)"
    )
    ap.add_argument("--force", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    sym = args.symbol.upper()
    if not args.force:
        ans = input(f"BUY {args.shares} shares of {sym} via IB Gateway? [y/N]: ").strip().lower()
        if ans != "y":
            print("aborted")
            return 0

    broker = IBKRClient()
    broker.connect()
    exec_log.wrap_broker(broker)
    try:
        current = broker.position(sym)
        target = current + args.shares
        rec = broker.rebalance(sym, target)
        exec_log.log(
            action="manual_buy",
            symbol=sym,
            payload=rec,
            notes=f"manual_buy qty={args.shares} via buy_position.py",
        )
        print(f"\nresult: {rec}")

        if not args.no_track and rec.get("status") == "submitted":
            pos_file = ROOT / "state" / "positions.json"
            positions = {}
            if pos_file.exists():
                try:
                    positions = json.loads(pos_file.read_text())
                except json.JSONDecodeError:
                    pass
            if sym not in positions:
                # Get current price for entry / stop
                try:
                    px = broker.market_price(sym)
                except Exception:
                    px = 0.0
                if px <= 0:
                    import yfinance as yf

                    try:
                        px = float(
                            yf.Ticker(sym).history(period="2d", interval="1d")["Close"].iloc[-1]
                        )
                    except Exception:
                        px = 0.0
                if px > 0:
                    from config import DEFAULT_STOP_PCT

                    positions[sym] = {
                        "symbol": sym,
                        "shares": target,
                        "entry": round(px, 2),
                        "stop": round(px * (1 - DEFAULT_STOP_PCT), 2),
                        "initial_stop": round(px * (1 - DEFAULT_STOP_PCT), 2),
                        "breakeven_moved": False,
                        "peak": round(px, 2),
                        "layer": 0,
                        "layer_entries": [round(px, 2)],
                        "entry_date": dt.date.today().isoformat(),
                        "manual_entry": True,
                    }
                    pos_file.parent.mkdir(parents=True, exist_ok=True)
                    pos_file.write_text(json.dumps(positions, indent=2))
                    print(
                        f"added {sym} to positions.json (entry={px:.2f}, stop={positions[sym]['stop']:.2f})"
                    )
    finally:
        broker.disconnect()
    return 0 if rec.get("status") == "submitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
