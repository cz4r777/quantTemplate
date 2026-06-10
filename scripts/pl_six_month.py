"""Compute 6-month P/L breakdown from a completed options-v1.2 backtest.

Reads state/backtest_options.json (produced by scripts/backtest_options.py)
and prints H1 (Jan-Jun) and H2 (Jul-Dec) returns per year.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def half(date_str: str) -> str:
    y, m = date_str[:4], int(date_str[5:7])
    return f"{y}-H1" if m <= 6 else f"{y}-H2"


def main() -> int:
    src = ROOT / "state" / "backtest_options.json"
    if not src.exists():
        print(f"ERROR: {src} not found. Run scripts/backtest_options.py first.", file=sys.stderr)
        return 1

    payload = json.loads(src.read_text())
    curve = payload.get("equity_curve") or []
    if not curve:
        print("ERROR: empty equity_curve", file=sys.stderr)
        return 1

    # Bucket equity rows by half-year. Preserve order.
    buckets: dict[str, list[dict]] = {}
    for row in curve:
        b = half(row["date"])
        buckets.setdefault(b, []).append(row)

    # Compute per-bucket P/L
    rows = []
    for b in sorted(buckets.keys()):
        s = buckets[b][0]["equity"]
        e = buckets[b][-1]["equity"]
        pnl = e - s
        ret_pct = (pnl / s * 100) if s else 0.0
        rows.append(
            {
                "period": b,
                "from": buckets[b][0]["date"],
                "to": buckets[b][-1]["date"],
                "start": s,
                "end": e,
                "pnl": pnl,
                "ret_pct": ret_pct,
                "trading_days": len(buckets[b]),
            }
        )

    # Print
    print("options-v1.2 — 6-month P/L breakdown")
    print(
        f"{'period':<8} {'from':<11} {'to':<11} {'start':>13} {'end':>13} {'P/L $':>13} {'P/L %':>8}  days"
    )
    print("-" * 92)
    for r in rows:
        tag = ""
        if r["ret_pct"] >= 25:
            tag = " STRONG"
        elif r["ret_pct"] <= -10:
            tag = " DRAWDOWN"
        elif r["ret_pct"] < 0:
            tag = " loss"
        print(
            f"{r['period']:<8} {r['from']:<11} {r['to']:<11} "
            f"${r['start']:>11,.0f} ${r['end']:>11,.0f} "
            f"${r['pnl']:>+11,.0f} {r['ret_pct']:>+7.2f}%  {r['trading_days']:>4}{tag}"
        )

    # Cumulative summary
    start = curve[0]["equity"]
    end = curve[-1]["equity"]
    print("-" * 92)
    print(
        f"cumulative: ${start:,.0f} -> ${end:,.0f}  P/L ${end - start:+,.0f}  {(end - start) / start * 100:+.1f}%"
    )

    # Also write a JSON sidecar so future sessions can re-render without re-reading the curve
    out = ROOT / "state" / "pl_six_month.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
