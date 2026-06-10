"""Run the Trend Template + RS + CAN SLIM filters across the watchlist.

Writes state/scan.json with pass/fail for each symbol.
Prints only a summary line. No per-symbol noise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.data_feed import fetch_ohlcv
from brain.fundamentals import can_slim_check
from brain.rs_rank import compute_ranks
from brain.stage_engine import trend_template
from config import LOOKBACK_DAYS, WATCHLIST_FILE

OUT = ROOT / "state" / "scan.json"


def load_tickers() -> list[str]:
    p = Path(WATCHLIST_FILE)
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("tickers", [])


def main(include_fundamentals: bool = True) -> int:
    tickers = load_tickers()
    if not tickers:
        print("no watchlist; run scripts/build_watchlist.py first", file=sys.stderr)
        return 1

    # 1. Fetch all OHLCV
    dfs: dict = {}
    fetch_failures: list[tuple[str, str]] = []
    for t in tickers:
        try:
            df = fetch_ohlcv(t, LOOKBACK_DAYS)
            if len(df) >= 200:
                dfs[t] = df
        except Exception as e:
            fetch_failures.append((t, type(e).__name__))
    if fetch_failures:
        print(
            f"scan_fetch_error: {len(fetch_failures)} ticker(s) failed; "
            f"sample={fetch_failures[:5]}",
            file=sys.stderr,
        )

    # 2. Compute RS ranks across the loaded universe
    ranks = compute_ranks(dfs)

    # 3. Trend Template per symbol
    results = {}
    passing_trend = []
    for t, df in dfs.items():
        rs = ranks.get(t)
        tt = trend_template(df, rs_rank=rs, symbol=t)
        results[t] = {"trend_template": tt.to_dict()}
        if tt.passes:
            passing_trend.append(t)

    # 4. Fundamentals on passing candidates only (expensive API calls)
    passing_all = []
    if include_fundamentals:
        for t in passing_trend:
            try:
                f = can_slim_check(t)
                results[t]["fundamentals"] = f.to_dict()
                if f.passes:
                    passing_all.append(t)
            except Exception as e:
                results[t]["fundamentals"] = {"error": str(e)}

    out = {
        "universe": len(dfs),
        "passed_trend_template": passing_trend,
        "passed_all": passing_all if include_fundamentals else None,
        "results": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, default=str))

    print(
        f"{len(dfs)} scanned | "
        f"{len(passing_trend)} pass trend template | "
        f"{len(passing_all) if include_fundamentals else '-'} pass all gates"
    )
    return 0


if __name__ == "__main__":
    fund = "--no-fundamentals" not in sys.argv
    raise SystemExit(main(include_fundamentals=fund))
