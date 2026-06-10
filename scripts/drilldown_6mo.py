"""6-month drill-down: compare strategy vs SPY per half-year.

Usage:
  python scripts/drilldown_6mo.py                 # reads state/backtest.json
  python scripts/drilldown_6mo.py --bench SPY     # benchmark ticker (default SPY)

Splits the replay period into H1 (Jan-Jun) and H2 (Jul-Dec) windows. For each:
  * strategy return (bar-to-bar equity)
  * benchmark return (bar-to-bar close)
  * pilot/exit/pyramid counts
  * fraction of days entries_allowed (gate on/off)
  * mean DD count when gate is OFF (why were we locked out?)
  * max drawdown in window
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.data_feed import fetch_ohlcv


def window_key(date_str: str) -> str:
    y = date_str[:4]
    m = int(date_str[5:7])
    return f"{y}-H{1 if m <= 6 else 2}"


def window_ends(date_str: str) -> tuple[str, str]:
    y = int(date_str[:4])
    m = int(date_str[5:7])
    return (f"{y}-01-01", f"{y}-06-30") if m <= 6 else (f"{y}-07-01", f"{y}-12-31")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="SPY")
    ap.add_argument("--state", default=str(ROOT / "state" / "backtest.json"))
    args = ap.parse_args()

    data = json.loads(Path(args.state).read_text())
    curve = data["equity_curve"]
    events = data["events"]
    if not curve:
        print("empty equity curve", file=sys.stderr)
        return 1

    # Bucket curve + events by H1/H2
    buckets: dict[str, list[dict]] = {}
    for row in curve:
        buckets.setdefault(window_key(row["date"]), []).append(row)

    event_buckets: dict[str, list[dict]] = {}
    for e in events:
        event_buckets.setdefault(window_key(e["date"]), []).append(e)

    # SPY benchmark over full window
    start_date = curve[0]["date"]
    end_date = curve[-1]["date"]
    lookback_days = 1800  # plenty of buffer
    spy = fetch_ohlcv(args.bench, lookback_days)
    spy = spy.loc[(spy.index >= start_date) & (spy.index <= end_date)]

    print(f"\n{'=' * 96}")
    print(f"6-MONTH DRILL-DOWN  |  strategy vs {args.bench}  |  {start_date} -> {end_date}")
    print(f"{'=' * 96}")
    print(
        f"{'window':>9}  {'strat%':>8}  {'bench%':>8}  {'alpha':>7}  "
        f"{'pilots':>6}  {'exits':>5}  {'pyr':>4}  {'gate_on%':>8}  {'maxDD%':>6}  {'note':<30}"
    )
    print("-" * 96)

    strat_total_1, bench_total_1 = 1.0, 1.0
    beats, misses = 0, 0
    for w in sorted(buckets.keys()):
        bw = buckets[w]
        s0, s1 = bw[0]["equity"], bw[-1]["equity"]
        strat_ret = (s1 / s0 - 1.0) * 100

        # Benchmark slice — use bench close on first + last date of strategy window
        w_start, w_end = window_ends(bw[0]["date"])
        bslice = spy.loc[(spy.index >= w_start) & (spy.index <= w_end)]
        if len(bslice) == 0:
            bench_ret = 0.0
        else:
            b0 = float(bslice["Close"].iloc[0])
            b1 = float(bslice["Close"].iloc[-1])
            bench_ret = (b1 / b0 - 1.0) * 100

        alpha = strat_ret - bench_ret
        if alpha >= 0:
            beats += 1
        else:
            misses += 1
        strat_total_1 *= 1 + strat_ret / 100
        bench_total_1 *= 1 + bench_ret / 100

        evs = event_buckets.get(w, [])
        act = Counter(e["action"] for e in evs)
        pilots = act.get("pilot", 0)
        exits = act.get("exit", 0)
        pyr = act.get("pyramid", 0)

        gate_on = sum(1 for r in bw if r.get("entries_allowed", True))
        gate_pct = gate_on / len(bw) * 100 if bw else 0

        # Max drawdown within window
        peak, worst = 0.0, 0.0
        for r in bw:
            peak = max(peak, r["equity"])
            if peak > 0:
                worst = min(worst, r["equity"] / peak - 1.0)
        max_dd = abs(worst) * 100

        # Diagnostic note: why might we have underperformed?
        note = ""
        if alpha < -3:
            # Inspect DD counts when gate was off
            dd_off = [r.get("dd_count", 0) for r in bw if not r.get("entries_allowed", True)]
            if dd_off:
                avg_dd = sum(dd_off) / len(dd_off)
                note = f"gate_off_avg_dd={avg_dd:.1f}"
            if pilots == 0:
                note = "no_entries"
            elif strat_ret < 0 and bench_ret > 0:
                note += " missed_bull" if note else "missed_bull"

        print(
            f"{w:>9}  {strat_ret:>+7.1f}%  {bench_ret:>+7.1f}%  "
            f"{alpha:>+6.1f}  {pilots:>6}  {exits:>5}  {pyr:>4}  "
            f"{gate_pct:>7.0f}%  {max_dd:>5.1f}%  {note:<30}"
        )

    strat_total = (strat_total_1 - 1) * 100
    bench_total = (bench_total_1 - 1) * 100
    print("-" * 96)
    print(
        f"{'TOTAL':>9}  {strat_total:>+7.1f}%  {bench_total:>+7.1f}%  "
        f"{strat_total - bench_total:>+6.1f}  "
        f"beats={beats} misses={misses}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
