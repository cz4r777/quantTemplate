"""Scan a broad universe for breakouts today.

Broader than our trading universe — includes FFTY + MAG7 + SP500 top-50 +
Nasdaq 100 so you can see the whole market's breakout tape, not just what
the bot will trade.

Reports every name showing ANY of:
  - Full breakout (close > 20d high + volume ≥ 1.4× avg + upper half)
  - Pocket pivot (up day vol > any down day vol in 10 sessions)
  - At/near 52-week high (within 1%)

Sorted by today's % move, descending. Run anytime:

    python scripts/find_breakouts.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brain.breakout import detect as detect_breakout
from brain.data_feed import fetch_ohlcv
from brain.rs_rank import compute_ranks
from brain.stage_engine import trend_template
from config import MAG7


def load_universe() -> list[str]:
    """Combine every list we know about."""
    symbols: set[str] = set(MAG7)

    for f in ("state/watchlist.json", "state/sp500_top50.json", "state/nasdaq100.json"):
        p = Path(f)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                symbols.update(data.get("tickers", []))
            except (json.JSONDecodeError, KeyError):
                pass

    if not Path("state/nasdaq100.json").exists():
        print(
            "note: state/nasdaq100.json missing — run scripts/build_nasdaq100.py to expand coverage"
        )

    return sorted(symbols)


def main() -> int:
    symbols = load_universe()
    if not symbols:
        print("no symbols to scan — run scripts/build_watchlist.py first", file=sys.stderr)
        return 1

    print(f"scanning {len(symbols)} symbols (this takes ~{len(symbols) // 3}s)...")
    t0 = time.time()
    dfs: dict = {}
    fetch_failures: list[tuple[str, str]] = []
    for s in symbols:
        try:
            df = fetch_ohlcv(s, 500)
            if len(df) >= 200:
                dfs[s] = df
        except Exception as e:
            fetch_failures.append((s, type(e).__name__))
    print(f"loaded {len(dfs)}/{len(symbols)} in {time.time() - t0:.1f}s")
    if fetch_failures:
        print(f"  skipped {len(fetch_failures)} symbols on fetch errors", file=sys.stderr)
    if not dfs:
        print(
            "breakout scan FAIL: no OHLCV data loaded; preserving last-good breakouts.json",
            file=sys.stderr,
        )
        return 1

    ranks = compute_ranks(dfs)
    breakouts = []

    for sym, df in dfs.items():
        last = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
        chg = ((last / prev) - 1.0) * 100 if prev > 0 else 0.0
        high52w = float(df["High"].tail(252).max())
        is_52w_high = last >= high52w * 0.99
        is_20d_high = last >= float(df["High"].tail(21).iloc[:-1].max())

        bo = detect_breakout(df, symbol=sym)
        tt = trend_template(df, rs_rank=ranks.get(sym), symbol=sym)

        if bo.is_breakout or bo.is_pocket_pivot or is_52w_high:
            breakouts.append(
                {
                    "sym": sym,
                    "last": last,
                    "chg": chg,
                    "52wh": is_52w_high,
                    "20dh": is_20d_high,
                    "bo": bo.is_breakout,
                    "pp": bo.is_pocket_pivot,
                    "tt": tt.passes,
                    "base": bo.base_count,
                    "rs": ranks.get(sym, 0),
                }
            )

    breakouts.sort(key=lambda x: x["chg"], reverse=True)

    print()
    print(
        f"{'SYM':<6} {'LAST':>8} {'CHG%':>7}  {'52wH':<4} {'20dH':<4} {'BO':<3} {'PP':<3} {'TT':<3} {'BASE':>4} {'RS':>4}"
    )
    print("-" * 68)
    for b in breakouts:
        print(
            f"{b['sym']:<6} "
            f"{b['last']:>8.2f} "
            f"{b['chg']:>+6.2f}%  "
            f"{('Y' if b['52wh'] else '.'):<4} "
            f"{('Y' if b['20dh'] else '.'):<4} "
            f"{('Y' if b['bo'] else '.'):<3} "
            f"{('Y' if b['pp'] else '.'):<3} "
            f"{('Y' if b['tt'] else '.'):<3} "
            f"{b['base']:>4} "
            f"{b['rs']:>4.0f}"
        )
    print()
    print(
        f"{len(breakouts)} names with breakout/high signal "
        f"(TT = passes Minervini Trend Template; BASE = base count, <=2 preferred)"
    )

    # Save for dashboard consumption
    Path("state/breakouts.json").write_text(
        json.dumps(
            {
                "as_of": time.strftime("%Y-%m-%d %H:%M"),
                "breakouts": breakouts,
            },
            indent=2,
            default=str,
        )
    )
    print("wrote state/breakouts.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
