"""Per-trade execution audit.

Reads state/backtest.json events and reconstructs round-trip trades with:
  * entry price, exit price, peak price, return %
  * capture ratio = (exit - entry) / (peak - entry)  — how much of the max
    favorable move we kept. <50% means we cut winners short.
  * hold duration in calendar days
  * exit reason (hard stop / trail / climax / stage_3 / etc.)

Outputs:
  * Summary: avg capture, win rate, distribution by exit reason, top
    winners and losers
  * Optional --window filter (YYYY-MM-DD..YYYY-MM-DD) to drill a specific
    period (e.g. 2023-07-01..2023-12-31 for 2023-H2)

Usage:
  python scripts/trade_analysis.py
  python scripts/trade_analysis.py --window 2023-07-01..2023-12-31
  python scripts/trade_analysis.py --top 10
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_window(s: str | None) -> tuple[str, str]:
    if not s:
        return "1900-01-01", "2100-01-01"
    a, b = s.split("..")
    return a, b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(ROOT / "state" / "backtest.json"))
    ap.add_argument("--window", help="YYYY-MM-DD..YYYY-MM-DD (by entry date)")
    ap.add_argument("--top", type=int, default=5, help="top winners/losers to list")
    args = ap.parse_args()

    data = json.loads(Path(args.state).read_text())
    events = data["events"]

    # Build round-trips: match pilot -> next exit for same symbol
    open_pos: dict[str, dict] = {}
    trips: list[dict] = []
    for e in events:
        sym = e.get("symbol")
        if not sym:
            continue
        if e["action"] == "pilot":
            open_pos[sym] = {
                "symbol": sym,
                "entry_date": e["date"],
                "entry_price": e.get("entry"),
            }
        elif e["action"] == "exit" and sym in open_pos:
            tp = open_pos.pop(sym)
            entry_p = tp["entry_price"] or e.get("entry_price")
            exit_p = e["price"]
            peak_p = e.get("peak_price") or exit_p
            if entry_p is None or entry_p <= 0:
                continue
            ret = (exit_p - entry_p) / entry_p * 100
            peak_gain = (peak_p - entry_p) / entry_p * 100
            if peak_gain > 0:
                capture = (exit_p - entry_p) / (peak_p - entry_p) * 100
            else:
                capture = 0.0
            try:
                d0 = dt.date.fromisoformat(tp["entry_date"])
                d1 = dt.date.fromisoformat(e["date"])
                hold_days = (d1 - d0).days
            except (ValueError, TypeError):
                hold_days = 0
            trips.append(
                {
                    "symbol": sym,
                    "entry_date": tp["entry_date"],
                    "exit_date": e["date"],
                    "entry": entry_p,
                    "exit": exit_p,
                    "peak": peak_p,
                    "return_pct": ret,
                    "peak_gain_pct": peak_gain,
                    "capture_pct": capture,
                    "hold_days": hold_days,
                    "exit_reason": e.get("reason", "unknown"),
                }
            )

    # Filter by window
    frm, to = parse_window(args.window)
    scope = [t for t in trips if frm <= t["entry_date"] <= to]

    if not scope:
        print(f"no round trips in window {frm}..{to}", file=sys.stderr)
        return 1

    # Summary
    wins = [t for t in scope if t["return_pct"] > 0]
    losses = [t for t in scope if t["return_pct"] <= 0]
    big_winners = [t for t in scope if t["peak_gain_pct"] >= 20]

    print(f"\n{'=' * 80}")
    print(f"TRADE EXECUTION AUDIT  |  window: {frm} -> {to}  |  trips: {len(scope)}")
    print(f"{'=' * 80}")
    print(
        f"{'win rate':<20} {len(wins) / len(scope) * 100:.0f}%  ({len(wins)} wins, {len(losses)} losses)"
    )
    print(
        f"{'avg win':<20} {sum(t['return_pct'] for t in wins) / len(wins):+.1f}%"
    ) if wins else None
    print(
        f"{'avg loss':<20} {sum(t['return_pct'] for t in losses) / len(losses):+.1f}%"
    ) if losses else None
    print(f"{'avg return all':<20} {sum(t['return_pct'] for t in scope) / len(scope):+.1f}%")
    print(f"{'avg peak gain':<20} {sum(t['peak_gain_pct'] for t in scope) / len(scope):+.1f}%")

    # Capture ratio is meaningful only on trades that had some positive peak
    capture_ratios = [t["capture_pct"] for t in scope if t["peak_gain_pct"] > 1]
    if capture_ratios:
        avg_capture = sum(capture_ratios) / len(capture_ratios)
        below_50 = sum(1 for c in capture_ratios if c < 50)
        print(f"{'avg capture ratio':<20} {avg_capture:.0f}%  (exit price / peak price)")
        print(
            f"{'trades capturing <50%':<20} {below_50}/{len(capture_ratios)}  "
            f"({below_50 / len(capture_ratios) * 100:.0f}%)"
        )

    # Big-winner capture specifically (trades that had >=20% peak gain)
    if big_winners:
        big_caps = [t["capture_pct"] for t in big_winners]
        print("\n--- BIG WINNERS (peak gain >=20%) ---")
        print(
            f"count: {len(big_winners)}   avg peak gain: "
            f"{sum(t['peak_gain_pct'] for t in big_winners) / len(big_winners):.1f}%"
        )
        print(f"avg capture of big winners: {sum(big_caps) / len(big_caps):.0f}%")
        cut_short = [t for t in big_winners if t["capture_pct"] < 50]
        print(f"big winners we cut short (<50% capture): {len(cut_short)}")

    # Hold duration distribution
    holds = [t["hold_days"] for t in scope]
    avg_hold = sum(holds) / len(holds)
    print(f"\n{'avg hold':<20} {avg_hold:.0f} days (min {min(holds)}, max {max(holds)})")

    # Exit reason breakdown
    print("\n--- EXIT REASONS ---")
    by_reason = defaultdict(list)
    for t in scope:
        # Normalize: "stop_hit:X<=Y" -> "stop_hit"
        key = t["exit_reason"].split(":")[0]
        by_reason[key].append(t)
    for reason, trs in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        avg_r = sum(t["return_pct"] for t in trs) / len(trs)
        avg_peak = sum(t["peak_gain_pct"] for t in trs) / len(trs)
        avg_cap = sum(t["capture_pct"] for t in trs if t["peak_gain_pct"] > 1) / max(
            1, sum(1 for t in trs if t["peak_gain_pct"] > 1)
        )
        print(
            f"  {reason:<25} n={len(trs):>3}  "
            f"avg ret {avg_r:+5.1f}%  avg peak {avg_peak:+5.1f}%  "
            f"capture {avg_cap:.0f}%"
        )

    # Top winners and losers
    by_ret = sorted(scope, key=lambda t: -t["return_pct"])
    print(f"\n--- TOP {args.top} WINNERS ---")
    for t in by_ret[: args.top]:
        print(
            f"  {t['symbol']:<6} {t['entry_date']} -> {t['exit_date']}  "
            f"{t['return_pct']:+6.1f}% (peak {t['peak_gain_pct']:+5.1f}%, "
            f"captured {t['capture_pct']:.0f}%)  "
            f"{t['hold_days']}d  {t['exit_reason']}"
        )

    print(f"\n--- TOP {args.top} LOSERS ---")
    for t in by_ret[-args.top :]:
        print(
            f"  {t['symbol']:<6} {t['entry_date']} -> {t['exit_date']}  "
            f"{t['return_pct']:+6.1f}% (peak {t['peak_gain_pct']:+5.1f}%, "
            f"captured {t['capture_pct']:.0f}%)  "
            f"{t['hold_days']}d  {t['exit_reason']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
