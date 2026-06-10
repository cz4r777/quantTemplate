"""Weekly gate-audit report: compare gate behavior vs actual SPY weekly action.

One row per week. Flags where gate said block but SPY was clearly trending up,
so the operator can review whether the gate misbehaved that week.

Usage:
  python scripts/gate_weekly_report.py                # writes state/gate_weekly.md
  python scripts/gate_weekly_report.py --only-flags   # show only flagged weeks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def iso_week(date_str: str) -> str:
    import datetime as dt

    y, m, d = (int(x) for x in date_str.split("-"))
    iso = dt.date(y, m, d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(ROOT / "state" / "backtest.json"))
    ap.add_argument("--out", default=str(ROOT / "state" / "gate_weekly.md"))
    ap.add_argument("--only-flags", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.state).read_text())
    curve = data["equity_curve"]
    if not curve:
        print("empty equity curve", file=sys.stderr)
        return 1

    # Bucket rows by ISO week
    weeks: dict[str, list[dict]] = {}
    for row in curve:
        weeks.setdefault(iso_week(row["date"]), []).append(row)

    out_lines: list[str] = []
    out_lines.append(f"# Gate Weekly Audit - {curve[0]['date']} -> {curve[-1]['date']}\n")
    out_lines.append(
        "One row per ISO week. FLAG column fires when the gate was off for the "
        "majority of the week yet SPY moved >0.5% (gate disagreement with market).\n"
    )
    out_lines.append("")
    header = (
        "| Week     | SPY start | SPY end | SPY%   | Gate on | DD avg | Regime common | "
        "Overrides | Pilots | Flag |"
    )
    sep = (
        "|----------|-----------|---------|--------|---------|--------|---------------|"
        "-----------|--------|------|"
    )
    out_lines.append(header)
    out_lines.append(sep)

    events = data.get("events", [])
    events_by_week: dict[str, list[dict]] = {}
    for e in events:
        events_by_week.setdefault(iso_week(e["date"]), []).append(e)

    flagged = 0
    total = 0
    for w in sorted(weeks.keys()):
        rows = weeks[w]
        total += 1
        spy_start = rows[0].get("spy_close", 0)
        spy_end = rows[-1].get("spy_close", 0)
        spy_pct = (spy_end / spy_start - 1) * 100 if spy_start > 0 else 0

        gate_on = sum(1 for r in rows if r.get("entries_allowed", True))
        gate_pct = gate_on / len(rows) * 100

        dd_avg = sum(r.get("dd_count", 0) for r in rows) / len(rows)

        from collections import Counter

        regime_counter = Counter(r.get("regime", "?") for r in rows)
        common_regime = regime_counter.most_common(1)[0][0]

        overrides = sum(1 for r in rows if r.get("sanity_violation"))
        pilots = sum(1 for e in events_by_week.get(w, []) if e.get("action") == "pilot")

        # Flag: gate was off majority of week AND SPY moved up >0.5% that week
        flag = ""
        if gate_pct < 50 and spy_pct > 0.5:
            flag = "DISAGREE"
            flagged += 1
        elif overrides > 0:
            flag = f"override x{overrides}"

        if args.only_flags and not flag:
            continue

        out_lines.append(
            f"| {w} | {spy_start:>9.2f} | {spy_end:>7.2f} | {spy_pct:>+5.1f}% | "
            f"{gate_pct:>5.0f}%  | {dd_avg:>5.1f}  | {common_regime:<13} | "
            f"{overrides:>9} | {pilots:>6} | {flag:<10} |"
        )

    out_lines.append("")
    out_lines.append(f"**Summary:** {flagged}/{total} weeks flagged as gate/market disagreement.")
    out_lines.append(
        "**DISAGREE** = gate was off >50% of the week while SPY rose >0.5% "
        "(possible gate malfunction)."
    )

    Path(args.out).write_text("\n".join(out_lines))
    print(f"wrote {args.out}")
    print(f"weeks: {total}, flagged: {flagged} ({flagged / total * 100:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
