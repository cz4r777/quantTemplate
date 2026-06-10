"""Run the backtest against the current FFTY watchlist.

Uses yfinance to pull the prior N days of OHLCV. Results are written to
state/backtest.json (equity curve, trades, events) and printed as a short
summary.

    python scripts/backtest.py --years 2
    python scripts/backtest.py --start 2024-01-01 --equity 50000 --fees 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest import engine
from backtest.stats import compute
from brain.data_feed import fetch_ohlcv
from brain.sector_strength import SECTOR_ETF
from config import LOOKBACK_DAYS, WATCHLIST_FILE


def load_tickers() -> list[str]:
    p = Path(WATCHLIST_FILE)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("tickers", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0, help="years of history to replay")
    ap.add_argument("--start", type=str, default=None, help="explicit start date YYYY-MM-DD")
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--fees", type=float, default=0.0, help="commission in bps per side")
    ap.add_argument(
        "--slippage",
        type=float,
        default=25.0,
        help="slippage in bps per side (default 25 — realistic for breakouts)",
    )
    ap.add_argument(
        "--fundamentals",
        dest="fundamentals",
        action="store_true",
        default=None,
        help="force-enable CAN SLIM filter (overrides config.APPLY_FUNDAMENTALS)",
    )
    ap.add_argument(
        "--no-fundamentals",
        dest="fundamentals",
        action="store_false",
        help="force-disable CAN SLIM filter",
    )
    args = ap.parse_args()

    tickers = load_tickers()
    if not tickers:
        print("no watchlist; run scripts/build_watchlist.py first", file=sys.stderr)
        return 1

    history_days = int(args.years * 252 + 250)  # add warmup buffer
    lookback = max(history_days, LOOKBACK_DAYS)

    sector_etfs = list(set(SECTOR_ETF.values()))
    all_symbols = tickers + sector_etfs
    print(f"loading {len(all_symbols)} symbols + SPY ({lookback} days)...")
    t0 = time.time()
    dfs: dict = {}
    fetch_failures: list[tuple[str, str]] = []
    for t in all_symbols:
        try:
            df = fetch_ohlcv(t, lookback)
            if len(df) >= 250:
                dfs[t] = df
        except Exception as e:
            fetch_failures.append((t, type(e).__name__))
    spy = fetch_ohlcv("SPY", lookback)
    print(f"loaded {len(dfs)}/{len(all_symbols)} symbols in {time.time() - t0:.1f}s")
    if fetch_failures:
        print(f"  skipped {len(fetch_failures)} symbols on fetch errors", file=sys.stderr)

    print("replaying...")
    t0 = time.time()
    last_shown = [0]

    def progress(i: int, total: int) -> None:
        if i == total or i - last_shown[0] >= max(total // 20, 1):
            print(f"  {i}/{total}")
            last_shown[0] = i

    result = engine.run(
        dfs=dfs,
        bench_df=spy,
        starting_equity=args.equity,
        commission_bps=args.fees,
        slippage_bps=args.slippage,
        start_date=args.start,
        progress=progress,
        apply_fundamentals=args.fundamentals,  # None = use config default
    )
    print(f"replayed {len(result.equity_curve)} days in {time.time() - t0:.1f}s")

    stats = compute(result)

    out_path = ROOT / "state" / "backtest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "stats": stats.to_dict(),
                "equity_curve": result.equity_curve,
                "events": result.events,
                "trades": result.trades,
            },
            indent=2,
            default=str,
        )
    )

    print()
    print(f"period          {result.equity_curve[0]['date']} -> {result.equity_curve[-1]['date']}")
    print(f"equity          ${stats.starting_equity:,.0f} -> ${stats.final_equity:,.0f}")
    print(f"total return    {stats.total_return_pct:+.1f}%")
    print(f"CAGR            {stats.cagr_pct:+.1f}%")
    print(f"max drawdown    -{stats.max_drawdown_pct:.1f}%")
    print(f"sharpe          {stats.sharpe:.2f}")
    print(f"trades          {stats.num_trades} ({stats.num_exits} round trips)")
    print(f"win rate        {stats.win_rate:.0f}%")
    print(
        f"avg win/loss    {stats.avg_win_pct:+.1f}% / {stats.avg_loss_pct:+.1f}%  (ratio {stats.avg_win_loss_ratio:.2f})"
    )
    print(f"time in market  {stats.time_in_market_pct:.0f}%")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
