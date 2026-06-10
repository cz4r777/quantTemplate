"""Diagnose why specific 6-month periods underperformed.

Reads state/backtest_options.json and produces a forensics breakdown for
each named period: how many days the gate was on/off, what gate reasons
blocked entries, how many trades fired, and what their P/L looked like.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Periods of interest (the weak halves the operator flagged)
PERIODS = {
    "2021-H1": ("2021-01-01", "2021-06-30"),
    "2023-H2": ("2023-07-01", "2023-12-31"),
    "2025-H1": ("2025-01-01", "2025-06-30"),
    # Reference: a STRONG half for comparison
    "2024-H1": ("2024-01-01", "2024-06-30"),
}


def in_period(date_str: str, lo: str, hi: str) -> bool:
    return lo <= date_str <= hi


def diagnose(curve, events, label, lo, hi):
    rows = [r for r in curve if in_period(r["date"], lo, hi)]
    if not rows:
        return None
    evs = [e for e in events if in_period(e["date"], lo, hi)]

    days = len(rows)
    on_days = sum(1 for r in rows if r.get("gate_inputs", {}).get("allow_entries"))
    off_days = days - on_days

    reason_counts = Counter()
    path_counts = Counter()
    sanity_violations = 0
    for r in rows:
        gi = r.get("gate_inputs", {}) or {}
        reason_counts[gi.get("reason", "?")] += 1
        path_counts[gi.get("path", "?")] += 1
        if gi.get("sanity_violation"):
            sanity_violations += 1

    opens = [e for e in evs if e["action"] == "open"]
    closes = [e for e in evs if e["action"] == "close"]
    trims = [e for e in evs if e["action"] == "trim"]

    close_pnl = [e.get("pnl", 0) for e in closes]
    wins = [p for p in close_pnl if p > 0]
    losses = [p for p in close_pnl if p <= 0]

    close_reasons = Counter(e.get("reason", "?") for e in closes)
    trim_reasons = Counter(e.get("reason", "?") for e in trims)

    start_eq = rows[0]["equity"]
    end_eq = rows[-1]["equity"]
    pnl_dollars = end_eq - start_eq
    ret_pct = (pnl_dollars / start_eq * 100) if start_eq else 0

    return {
        "label": label,
        "from": rows[0]["date"],
        "to": rows[-1]["date"],
        "trading_days": days,
        "gate_on_days": on_days,
        "gate_off_days": off_days,
        "gate_on_pct": round(on_days / days * 100, 1),
        "sanity_violations": sanity_violations,
        "gate_reason_top": reason_counts.most_common(5),
        "gate_path": dict(path_counts),
        "opens": len(opens),
        "closes": len(closes),
        "trims": len(trims),
        "close_wins": len(wins),
        "close_losses": len(losses),
        "close_pnl_sum": round(sum(close_pnl), 0),
        "avg_win": round(sum(wins) / len(wins), 0) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 0) if losses else 0,
        "close_reasons": dict(close_reasons),
        "trim_reasons": dict(trim_reasons),
        "start_eq": start_eq,
        "end_eq": end_eq,
        "pnl_dollars": round(pnl_dollars, 0),
        "ret_pct": round(ret_pct, 2),
    }


def print_report(reports):
    for r in reports:
        if r is None:
            continue
        print(f"\n{'=' * 88}")
        print(
            f"{r['label']}   {r['from']} -> {r['to']}   {r['ret_pct']:+.2f}%   ${r['pnl_dollars']:+,.0f}"
        )
        print("=" * 88)
        print(f"  trading days     {r['trading_days']}")
        print(f"  gate ON          {r['gate_on_days']:>4} ({r['gate_on_pct']}%)")
        print(f"  gate OFF         {r['gate_off_days']:>4}")
        print(f"  sanity violations {r['sanity_violations']}  (= obvious_bull override fired)")
        print(f"  gate paths       {r['gate_path']}")
        print(f"  top gate reasons {r['gate_reason_top']}")
        print("  ----")
        print(f"  trades opened    {r['opens']}")
        print(
            f"  closes           {r['closes']}  (wins {r['close_wins']} / losses {r['close_losses']})"
        )
        print(f"  trims            {r['trims']}")
        print(f"  close P/L sum    ${r['close_pnl_sum']:+,.0f}")
        print(f"  avg win / loss   ${r['avg_win']:+,.0f} / ${r['avg_loss']:+,.0f}")
        print(f"  close reasons    {r['close_reasons']}")
        print(f"  trim reasons     {r['trim_reasons']}")
        print(f"  start / end eq   ${r['start_eq']:,.0f} -> ${r['end_eq']:,.0f}")


def main() -> int:
    src = ROOT / "state" / "backtest_options.json"
    payload = json.loads(src.read_text())
    curve = payload["equity_curve"]
    events = payload["events"]

    reports = []
    for label, (lo, hi) in PERIODS.items():
        reports.append(diagnose(curve, events, label, lo, hi))

    print_report(reports)

    out = ROOT / "state" / "diagnose_periods.json"
    out.write_text(json.dumps(reports, indent=2, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
