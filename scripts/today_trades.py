"""Today's IB fills — fast, focused report.

Connects to IB, pulls fills, filters to today (or --date), prints
buy/sell breakdown with totals. Use this when you want "what trades
happened today" without the full daily dump.

Usage:
  python scripts/today_trades.py
  python scripts/today_trades.py --date 2026-04-30
  python scripts/today_trades.py --days 3      # last N days
"""

from __future__ import annotations

import argparse
import datetime as dt
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="filter to this date (YYYY-MM-DD), default today",
    )
    ap.add_argument(
        "--days", type=int, default=2, help="how many days back to fetch from IB (default 2)"
    )
    args = ap.parse_args()

    target = args.date

    b = IBKRClient()
    b.connect()
    try:
        all_fills = b.fills(days=args.days)
    finally:
        b.disconnect()

    fills = [f for f in all_fills if str(f["time"]).startswith(target)]

    print("=" * 78)
    print(f"  IB FILLS ON {target}    ({len(fills)} fills)")
    print("=" * 78)

    if not fills:
        print("  (no fills)")
        return 0

    print(
        f"  {'time':<19}  {'sym':<6}  {'side':<4}  {'qty':>5}  "
        f"{'price':>9}  {'comm':>6}  {'P&L':>10}"
    )
    print("  " + "-" * 74)

    n_buy = n_sell = 0
    vol_buy = vol_sell = 0
    total_pnl = 0.0
    total_comm = 0.0

    for f in fills:
        ts = f["time"][:19]
        sym = f["symbol"]
        side = "BUY" if f["side"] == "BOT" else "SELL"
        sh = f["shares"]
        px = f["price"]
        comm = f["commission"]
        pnl = f["realized_pnl"]
        if side == "BUY":
            n_buy += 1
            vol_buy += sh
        else:
            n_sell += 1
            vol_sell += sh
        total_pnl += pnl
        total_comm += comm
        mk = "+" if pnl > 0 else ("-" if pnl < 0 else " ")
        print(
            f"  {ts:<19}  {sym:<6}  {side:<4}  {sh:>5}  "
            f"${px:>7.2f}  ${comm:>4.2f}  {mk}${abs(pnl):>8.2f}"
        )

    print("  " + "-" * 74)
    print(f"  buys:  {n_buy:>3} fills, {vol_buy:>5} shares")
    print(f"  sells: {n_sell:>3} fills, {vol_sell:>5} shares")
    print(f"  net:   {n_buy + n_sell:>3} fills, {vol_buy - vol_sell:+5} shares (buys minus sells)")
    print(f"  realized P&L: ${total_pnl:+,.2f}")
    print(f"  commissions:  ${total_comm:,.2f}")
    print(f"  net of comm:  ${total_pnl - total_comm:+,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
