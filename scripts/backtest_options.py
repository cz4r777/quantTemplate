"""Run the options-v1.2 backtest. Same entry signals as v1.2 stock (unified
`evaluate_gate()`: DD-counter redesign, obvious_bull override, GateDecision
audit payload, breakout filter tightening); BS-priced options.

v1.2 delta vs v1.1:
  - brain/gate.py drives gate decisions (single source of truth)
  - tighter breakout filter (0.5% clearance + 6% bar range)
  - DD-counter loosened (−0.5% threshold, 6/8 step counts) — fewer false correction signals
  - obvious_bull override: SPY > rising 200-DMA AND mt_state ≠ correction → entries fire
    even when HMM/DD say no (sanity_violation logged)
  - equity_curve rows now include `gate_inputs`, `gate_reason`, `sanity_violation`
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest import options_engine
from brain.data_feed import fetch_ohlcv, fetch_ohlcv_diag
from brain.sector_strength import SECTOR_ETF
from config import SYMBOLS, WATCHLIST_FILE


def compute_stats(result):
    curve = result.equity_curve
    if not curve:
        return {}
    start = result.starting_equity
    end = result.final_equity
    total_ret = (end - start) / start * 100
    days = len(curve)
    years = max(days / 252, 1 / 252)
    cagr = ((end / start) ** (1 / years) - 1) * 100

    peak, worst = 0.0, 0.0
    for row in curve:
        peak = max(peak, row["equity"])
        if peak > 0:
            dd = (row["equity"] - peak) / peak
            worst = min(worst, dd)
    max_dd = abs(worst) * 100

    # Round-trip analysis from events
    opens = [e for e in result.events if e["action"] == "open"]
    round_trips = []
    for e in result.events:
        if e["action"] == "close" and "pnl" in e:
            round_trips.append(e["pnl"])
    wins = [p for p in round_trips if p > 0]
    losses = [p for p in round_trips if p <= 0]
    win_rate = (len(wins) / len(round_trips) * 100) if round_trips else 0

    # Yearly breakdown
    by_year: dict = {}
    for row in curve:
        y = str(row["date"])[:4]
        by_year.setdefault(y, []).append(row["equity"])
    yearly = []
    for y in sorted(by_year.keys()):
        s, e = by_year[y][0], by_year[y][-1]
        yearly.append(
            {
                "year": y,
                "start": round(s, 2),
                "end": round(e, 2),
                "return_pct": round((e - s) / s * 100, 2) if s > 0 else 0,
            }
        )

    return {
        "starting_equity": start,
        "final_equity": end,
        "total_return_pct": round(total_ret, 1),
        "cagr_pct": round(cagr, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "trades_opened": len(opens),
        "round_trips": len(round_trips),
        "win_rate": round(win_rate, 1),
        "avg_pnl_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_pnl_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "trading_days": days,
        "yearly_returns": yearly,
    }


def load_tickers() -> list[str]:
    p = Path(WATCHLIST_FILE)
    if not p.exists():
        print(f"watchlist missing; using fallback symbols: {', '.join(SYMBOLS)}", file=sys.stderr)
        return list(SYMBOLS)
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        print(
            f"watchlist unreadable ({e}); using fallback symbols: {', '.join(SYMBOLS)}",
            file=sys.stderr,
        )
        return list(SYMBOLS)
    tickers = data.get("tickers") or []
    if not tickers:
        print(f"watchlist empty; using fallback symbols: {', '.join(SYMBOLS)}", file=sys.stderr)
        return list(SYMBOLS)
    return tickers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--equity", type=float, default=100_000.0)
    args = ap.parse_args()

    tickers = load_tickers()
    if not tickers:
        print("no watchlist", file=sys.stderr)
        return 1

    lookback = int(args.years * 252 + 250)
    sector_etfs = list(set(SECTOR_ETF.values()))
    print(f"loading {len(tickers)} + {len(sector_etfs)} sector ETFs + SPY...")
    t0 = time.time()
    dfs = {}
    # T-P1-YF1 — classify every drop so backtest variance from yfinance
    # non-determinism becomes diagnosable instead of silent.
    drops: list[dict] = []
    for t in tickers + sector_etfs:
        df, outcome = fetch_ohlcv_diag(t, lookback)
        if outcome["ok"] and len(df) >= 250:
            dfs[t] = df
            continue
        if outcome["ok"] and outcome["rows_returned"] < 250:
            reason = f"short_history ({outcome['rows_returned']} bars)"
            event = "yf_short_history"
        elif outcome["error_class"]:
            reason = f"{outcome['error_class']}: {outcome['error_msg']}"
            event = "yf_fetch_failed"
        else:
            reason = "empty"
            event = "yf_fetch_failed"
        drops.append(
            {
                "symbol": t,
                "reason": reason,
                "attempts": outcome["attempts"],
                "transient": outcome["transient"],
            }
        )
        print(
            f"{event}: {t} reason={reason} "
            f"attempts={outcome['attempts']} "
            f"transient={outcome['transient']}",
            file=sys.stderr,
        )
    spy = fetch_ohlcv("SPY", lookback)
    print(f"loaded {len(dfs)} of {len(tickers) + len(sector_etfs)} in {time.time() - t0:.1f}s")
    if drops:
        sample = "; ".join(f"{d['symbol']} {d['reason']}" for d in drops[:10])
        more = f" (+{len(drops) - 10} more)" if len(drops) > 10 else ""
        print(f"  dropped: {sample}{more}")

    t0 = time.time()
    last_shown = [0]

    def progress(i, total):
        if i == total or i - last_shown[0] >= max(total // 20, 1):
            print(f"  {i}/{total}")
            last_shown[0] = i

    result = options_engine.run(
        dfs=dfs,
        bench_df=spy,
        starting_equity=args.equity,
        start_date=args.start,
        progress=progress,
    )
    print(f"replayed {len(result.equity_curve)} days in {time.time() - t0:.1f}s")
    if not result.equity_curve:
        print("backtest produced no equity curve; check data load and start date", file=sys.stderr)
        return 1

    stats = compute_stats(result)

    out = ROOT / "state" / "backtest_options.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "stats": stats,
                "equity_curve": result.equity_curve,
                "events": result.events,
            },
            indent=2,
            default=str,
        )
    )

    print()
    print(f"period         {result.equity_curve[0]['date']} -> {result.equity_curve[-1]['date']}")
    print(f"equity         ${stats['starting_equity']:,.0f} -> ${stats['final_equity']:,.0f}")
    print(f"total return   {stats['total_return_pct']:+.1f}%")
    print(f"CAGR           {stats['cagr_pct']:+.1f}%")
    print(f"max drawdown   -{stats['max_drawdown_pct']:.1f}%")
    print(f"trades         {stats['trades_opened']} ({stats['round_trips']} closed)")
    print(f"win rate       {stats['win_rate']:.0f}%")
    print(f"avg pnl (win)  ${stats['avg_pnl_win']:,.0f}")
    print(f"avg pnl (loss) ${stats['avg_pnl_loss']:,.0f}")

    if stats.get("yearly_returns"):
        print("\nYearly breakdown:")
        for yr in stats["yearly_returns"]:
            tag = "  STRONG" if yr["return_pct"] > 25 else "  weak" if yr["return_pct"] < 5 else ""
            print(
                f"  {yr['year']}:  {yr['return_pct']:+7.2f}%   "
                f"(${yr['start']:>11,.0f} -> ${yr['end']:>11,.0f}){tag}"
            )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
