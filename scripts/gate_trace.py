"""Gate-decision drill-down: explain WHY the gate was on/off each day in a window.

Prints one row per trading day in the requested window. Columns show each
gate input (DD count, HMM regime, SPY vs MA distances, 50-DMA rising) and
the final decision path. Use this to find the *specific* input that's
tripping the gate in a bull-disagreement week.

Usage:
  python scripts/gate_trace.py --week 2025-W15
  python scripts/gate_trace.py --from 2025-04-07 --to 2025-04-11
  python scripts/gate_trace.py --last-n 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_iso_week(s: str) -> tuple[str, str]:
    # e.g. "2025-W15" -> (Monday date, Friday date)
    y, w = s.split("-W")
    mon = dt.date.fromisocalendar(int(y), int(w), 1)
    fri = mon + dt.timedelta(days=4)
    return mon.isoformat(), fri.isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(ROOT / "state" / "backtest.json"))
    ap.add_argument("--week", help="ISO week like 2025-W15")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to", dest="to")
    ap.add_argument("--last-n", type=int)
    args = ap.parse_args()

    data = json.loads(Path(args.state).read_text())
    curve = data["equity_curve"]

    if args.week:
        frm, to = parse_iso_week(args.week)
    elif args.frm and args.to:
        frm, to = args.frm, args.to
    else:
        frm, to = "1900-01-01", "2100-01-01"

    if args.last_n:
        rows = curve[-args.last_n :]
    else:
        rows = [r for r in curve if frm <= r["date"] <= to]

    if not rows:
        print(f"no rows in window {frm}..{to}", file=sys.stderr)
        return 1

    print(f"\nGate trace: {rows[0]['date']} -> {rows[-1]['date']} ({len(rows)} days)\n")
    print(
        f"{'date':10}  {'spy':>7}  {'21%':>6}  {'50%':>6}  {'200%':>6}  "
        f"{'50r':>3}  {'dd':>3}  {'mt':18}  {'hmm':9}  {'ob':>3}  "
        f"{'gate':>4}  reason"
    )
    print("-" * 120)

    for r in rows:
        gi = r.get("gate_inputs", {})
        spy = gi.get("spy_price", r.get("spy_close", 0))
        v21 = gi.get("spy_vs_21dma_pct")
        v50 = gi.get("spy_vs_50dma_pct")
        v200 = gi.get("spy_vs_200dma_pct")
        rising = "Y" if gi.get("ma50_rising") else "N"
        dd = gi.get("dd_count", r.get("dd_count", 0))
        mt = gi.get("mt_state", "?")
        hmm = gi.get("hmm_regime", r.get("regime", "?"))
        ob = "Y" if r.get("obvious_bull") else "N"
        allowed = "ON" if r.get("entries_allowed") else "OFF"
        reason = r.get("gate", "")

        def fmt(x, w=6, p=1):
            if x is None:
                return " " * w
            return f"{x:>+{w}.{p}f}"

        print(
            f"{r['date']:10}  {spy:>7.2f}  {fmt(v21)}  {fmt(v50)}  {fmt(v200)}  "
            f"{rising:>3}  {dd:>3}  {mt:18}  {hmm:9}  {ob:>3}  "
            f"{allowed:>4}  {reason}"
        )

    # Summary
    on_days = sum(1 for r in rows if r.get("entries_allowed"))
    print()
    print(f"Gate on: {on_days}/{len(rows)} days ({on_days / len(rows) * 100:.0f}%)")
    print()
    print("Legend: 21% / 50% / 200% = SPY % above/below that DMA")
    print("        50r = 50-DMA rising (Y/N)")
    print("        mt  = market_timing state (confirmed_uptrend / under_pressure / correction)")
    print("        hmm = HMM regime  |  ob = obvious-bull override fired  |  gate = final decision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
