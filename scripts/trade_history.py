"""Show recent IB fills + realized P&L (the 'where did my money go' tool).

Connects to IB Gateway, pulls execution history for the last N days,
prints a formatted table + totals + account summary.

Usage:
  python scripts/trade_history.py            # last 7 days
  python scripts/trade_history.py --days 1   # today only
  python scripts/trade_history.py --days 30
"""

from __future__ import annotations

import argparse
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
    ap.add_argument("--days", type=int, default=7, help="lookback window")
    args = ap.parse_args()

    broker = IBKRClient()
    broker.connect()
    try:
        fills = broker.fills(days=args.days)
        summary = broker.account_summary()
        # T-ACCOUNT-SUMMARY-SIDECAR1: capture account_id while the
        # connection is open so the written envelope can identify which
        # account this summary came from.
        snapshot_account_id: str | None = None
        try:
            managed = broker.ib.managedAccounts() or []
            if managed:
                snapshot_account_id = str(managed[0]).strip() or None
        except Exception:
            snapshot_account_id = None
    finally:
        broker.disconnect()

    print("\n=== Account summary (live from IB) ===")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k:<22} ${v:>14,.2f}")
        else:
            print(f"  {k:<22} {v}")

    print(f"\n=== Fills — last {args.days} days ({len(fills)} executions) ===")
    if not fills:
        print("  (no fills in this window)")
        return 0

    print(
        f"  {'time':<22} {'sym':<6} {'side':>4} {'shares':>7} {'price':>9} "
        f"{'commission':>11} {'realized P&L':>14}"
    )
    print("  " + "-" * 80)

    total_pnl = 0.0
    total_comm = 0.0
    by_symbol = {}
    for f in fills:
        comm = f["commission"]
        pnl = f["realized_pnl"]
        total_pnl += pnl
        total_comm += comm
        by_symbol.setdefault(f["symbol"], {"pnl": 0.0, "comm": 0.0, "n": 0})
        by_symbol[f["symbol"]]["pnl"] += pnl
        by_symbol[f["symbol"]]["comm"] += comm
        by_symbol[f["symbol"]]["n"] += 1
        time_str = f["time"][:19]
        marker = "+" if pnl > 0 else ("-" if pnl < 0 else " ")
        print(
            f"  {time_str:<22} {f['symbol']:<6} {f['side']:>4} "
            f"{f['shares']:>7} ${f['price']:>8.2f} ${comm:>9.2f}  "
            f"{marker}${abs(pnl):>11.2f}"
        )

    print("  " + "-" * 80)
    print(
        f"  {'TOTAL':<22} {'':<6} {'':>4} {'':>7} {'':>9} ${total_comm:>9.2f}  ${total_pnl:+12.2f}"
    )

    if by_symbol:
        print("\n=== By symbol ===")
        rows = sorted(by_symbol.items(), key=lambda x: x[1]["pnl"])
        for sym, agg in rows:
            print(
                f"  {sym:<6} {agg['n']:>3} fills  P&L ${agg['pnl']:+10.2f}  "
                f"commission ${agg['comm']:>7.2f}"
            )

    # Cache to disk so the dashboard can render it without hitting IB each time
    import json as _json

    out_dir = ROOT / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trade_history.json").write_text(
        _json.dumps(
            {
                "fills": fills,
                "summary": {
                    "total_pnl": round(total_pnl, 2),
                    "total_commission": round(total_comm, 2),
                    "fill_count": len(fills),
                    "by_symbol": {
                        k: {
                            kk: round(vv, 2) if isinstance(vv, float) else vv
                            for kk, vv in v.items()
                        }
                        for k, v in by_symbol.items()
                    },
                    "window_days": args.days,
                },
            },
            indent=2,
        )
    )
    # T-ACCOUNT-SUMMARY-SIDECAR1: write the envelope shape so dashboards
    # can show "from account X at <ts>" instead of just numeric fields.
    import datetime as _dt
    import os as _os

    (out_dir / "account_summary.json").write_text(
        _json.dumps(
            {
                "schema_version": 1,
                "account_id": snapshot_account_id,
                "as_of": _dt.datetime.now(_dt.UTC).isoformat(),
                "ibkr_mode": (_os.getenv("IBKR_MODE", "").strip().lower() or None),
                "summary": summary,
            },
            indent=2,
        )
    )
    print("\nwrote state/trade_history.json + state/account_summary.json")
    print("dashboard endpoints: /trades  /account")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
