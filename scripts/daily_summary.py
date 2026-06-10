"""Daily SMS summary — posts a brief account snapshot to smsbot at end of day.

Pulls live data from IB Gateway:
  * NetLiquidation
  * RealizedPnL (today)
  * UnrealizedPnL
  * Top 3 winners and losers
  * Number of open positions
  * Trades fired today

Sends as one short SMS via the smsbot.

Usage:
  python scripts/daily_summary.py             # send now
  python scripts/daily_summary.py --dry-run   # print to stdout, don't send
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

from notifications.ntfy import send as ntfy_send

from broker.ibkr_client import IBKRClient
from notifications.smsbot import send as sms_send


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't send SMS")
    args = ap.parse_args()

    broker = IBKRClient()
    broker.connect()
    try:
        summary = broker.account_summary()
        fills = broker.fills(days=1)
        positions = []
        for p in broker.ib.positions():
            shares = int(p.position)
            if shares == 0:
                continue
            sym = p.contract.symbol
            try:
                px = broker.market_price(sym)
            except Exception:
                px = 0.0
            cost = float(p.avgCost or 0)
            pnl = (px - cost) * shares if cost > 0 and px > 0 else 0.0
            pct = (px / cost - 1) * 100 if cost > 0 else 0.0
            positions.append({"sym": sym, "shares": shares, "pnl": pnl, "pct": pct})
    finally:
        broker.disconnect()

    nlv = summary.get("NetLiquidation", 0.0)
    realized = summary.get("RealizedPnL", 0.0)
    unrealized = summary.get("UnrealizedPnL", 0.0)
    cash = summary.get("TotalCashValue", 0.0)

    positions_sorted = sorted(positions, key=lambda x: x["pnl"], reverse=True)
    winners = [p for p in positions_sorted if p["pnl"] > 0][:3]
    losers = [p for p in positions_sorted[::-1] if p["pnl"] < 0][:3]

    today = dt.date.today().isoformat()
    n_pos = len(positions)
    n_fills = len(fills)

    lines = [
        f"v1.2 daily {today}",
        f"NLV ${nlv:,.0f}  Cash ${cash:,.0f}",
        f"Realized ${realized:+,.0f}  Unrealized ${unrealized:+,.0f}",
        f"{n_pos} positions, {n_fills} fills today",
    ]
    if winners:
        lines.append(
            "Winners: "
            + ", ".join(f"{p['sym']} ${p['pnl']:+,.0f}({p['pct']:+.0f}%)" for p in winners)
        )
    if losers:
        lines.append(
            "Losers:  "
            + ", ".join(f"{p['sym']} ${p['pnl']:+,.0f}({p['pct']:+.0f}%)" for p in losers)
        )

    msg = "\n".join(lines)

    if args.dry_run:
        print(msg)
    else:
        # SMS via local smsbot (legacy)
        sms_send(msg, category="status")
        # Push via ntfy.sh (new — set NTFY_TOPIC in .env to enable)
        title = f"v1.2 EOD {today}"
        priority = "high" if abs(realized) > 1000 or abs(unrealized) > 2000 else "default"
        tags = "money_with_wings" if (realized + unrealized) > 0 else "chart_with_downwards_trend"
        ntfy_send(msg, title=title, priority=priority, tags=tags)
        print("sent:")
        print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
