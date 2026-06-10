"""Smoke-test the new modules without touching IBKR.

Exercises: stage_engine + rs_rank + breakout + position_manager + heat calc.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from allocation.position_sizer import full_position_shares
from brain.breakout import detect as detect_breakout
from brain.data_feed import fetch_ohlcv
from brain.earnings_filter import blackout as earnings_blackout
from brain.market_timing import assess as assess_market_timing
from brain.rs_rank import compute_ranks
from brain.stage_engine import trend_template
from config import PILOT_FRACTIONS
from execution import exits as exit_rules
from execution import position_manager as pm
from safety.portfolio_heat import compute_heat

TEST_SYMS = ["NVDA", "MU", "AAPL", "TLT", "SPY"]
EQUITY = 100_000.0


def main() -> int:
    dfs = {}
    fetch_failures: list[tuple[str, str]] = []
    for s in TEST_SYMS:
        try:
            dfs[s] = fetch_ohlcv(s, 500)
        except Exception as e:
            fetch_failures.append((s, type(e).__name__))
    if fetch_failures:
        print(f"fetch warnings: {fetch_failures}", file=sys.stderr)

    ranks = compute_ranks(dfs)

    print(f"{'sym':<6} {'RS':>4} {'trend':>5} {'bkout':>5} {'pocket':>6} {'bases':>5}")
    print("-" * 40)
    for s, df in dfs.items():
        tt = trend_template(df, rs_rank=ranks.get(s), symbol=s)
        bo = detect_breakout(df, symbol=s)
        print(
            f"{s:<6} "
            f"{ranks.get(s, 0):>4.0f} "
            f"{'Y' if tt.passes else 'N':>5} "
            f"{'Y' if bo.is_breakout else 'N':>5} "
            f"{'Y' if bo.is_pocket_pivot else 'N':>6} "
            f"{bo.base_count:>5}"
        )

    # Market timing on SPY
    spy = dfs.get("SPY")
    if spy is not None:
        mt = assess_market_timing(spy)
        print(
            f"\nSPY market timing: state={mt.state} dd={mt.distribution_days} ftd={mt.is_ftd_today}"
        )

    # Earnings filter — check one
    blocked, reason = earnings_blackout("NVDA", strict=False)
    print(f"NVDA earnings blackout: blocked={blocked} ({reason})")

    # Position-manager + exits sanity
    print("\nposition_manager + exits:")
    positions = {}
    df = dfs.get("NVDA")
    if df is not None:
        price = float(df["Close"].iloc[-1])
        stop = pm.compute_initial_stop(price, float(df["Low"].tail(10).min()))
        full = full_position_shares(EQUITY, price, stop)
        pilot = int(full * PILOT_FRACTIONS[0])
        pm.open_pilot("NVDA", price, pilot, stop, positions)
        import pandas as pd

        positions["NVDA"]["entry_date"] = pd.Timestamp.utcnow().date().isoformat()
        print(f"  open NVDA pilot: {pilot} sh @ {price:.2f} stop {stop:.2f}")
        update = pm.update_stop(positions["NVDA"], df)
        if update:
            print(f"  stop update: {update.new_stop:.2f} ({update.reason})")
        exit_rules.mark_fast_runner(positions["NVDA"], df)
        sig = exit_rules.evaluate(positions["NVDA"], df)
        print(f"  exit signal: {sig}")
        heat = compute_heat(positions, EQUITY)
        print(f"  portfolio heat: {heat:.2%}")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
