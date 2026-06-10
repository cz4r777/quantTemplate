"""Iterative rule tuner with rollback.

Runs baseline backtest, then tries one-parameter variations. Keeps a change
if it improves the composite score; rolls back otherwise. Writes the best
config to state/tuning_history.json and applies it via a patch to config.py.

Score: weighted composite of CAGR, Sharpe, and max-drawdown.
  score = CAGR * 1.0 + Sharpe * 5.0 - max_DD * 0.5
  (Rewards return and risk-adjusted return; penalizes drawdown modestly.)

Usage:
    python scripts/tune.py --years 5 --fees 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


import config as cfg_module
from allocation import position_sizer as sizer_module
from backtest import engine
from backtest.stats import compute
from brain import fundamentals as fundamentals_module
from brain.data_feed import fetch_ohlcv
from brain.sector_strength import SECTOR_ETF
from config import WATCHLIST_FILE
from execution import exits as exits_module
from execution import position_manager as pm_module


def score(stats) -> float:
    """Composite: rewards CAGR + Sharpe, mildly penalizes drawdown."""
    return stats.cagr_pct * 1.0 + stats.sharpe * 5.0 - stats.max_drawdown_pct * 0.5


def snapshot_state(*modules) -> dict:
    """Capture tunable globals from a set of modules."""
    return {
        id(m): {k: getattr(m, k) for k in dir(m) if k.isupper() and not k.startswith("_")}
        for m in modules
    }


def restore_state(snapshot: dict, *modules) -> None:
    for m in modules:
        orig = snapshot.get(id(m), {})
        for k, v in orig.items():
            setattr(m, k, v)


def apply_overrides(overrides: dict, *modules) -> None:
    """Set attributes on EVERY module that has the key — `from config import X`
    creates a local name at import time, so we must patch each consumer."""
    for k, v in overrides.items():
        for m in modules:
            if hasattr(m, k):
                setattr(m, k, v)


def load_data(years: float):
    tickers = json.loads(Path(WATCHLIST_FILE).read_text()).get("tickers", [])
    if not tickers:
        raise SystemExit("no watchlist; run scripts/build_watchlist.py first")
    lookback = int(years * 252 + 250)
    sector_etfs = list(set(SECTOR_ETF.values()))

    print(f"loading {len(tickers)} + {len(sector_etfs)} sector ETFs + SPY...")
    t0 = time.time()
    dfs = {}
    fetch_failures: list[tuple[str, str]] = []
    for t in tickers + sector_etfs:
        try:
            df = fetch_ohlcv(t, lookback)
            if len(df) >= 250:
                dfs[t] = df
        except Exception as e:
            fetch_failures.append((t, type(e).__name__))
    spy = fetch_ohlcv("SPY", lookback)
    print(f"loaded {len(dfs)} symbols in {time.time() - t0:.1f}s")
    if fetch_failures:
        print(f"  skipped {len(fetch_failures)} symbols on fetch errors", file=sys.stderr)

    # Warm fundamentals cache
    print("warming fundamentals cache (one yfinance hit per ticker, cached 7d)...")
    t0 = time.time()
    fundamentals_module.warm_cache(list(dfs.keys()))
    print(f"cache ready in {time.time() - t0:.1f}s")

    return dfs, spy


def run_experiment(name: str, overrides: dict, dfs, spy, years: float, fees: float):
    """Apply overrides, run backtest, compute stats. Auto-restores on return."""
    modules = (cfg_module, pm_module, exits_module, sizer_module, engine)
    snapshot = snapshot_state(*modules)
    try:
        apply_overrides(overrides, *modules)
        t0 = time.time()
        result = engine.run(
            dfs=dfs,
            bench_df=spy,
            starting_equity=100_000.0,
            commission_bps=fees,
        )
        elapsed = time.time() - t0
        stats = compute(result)
        return {
            "name": name,
            "overrides": overrides,
            "stats": stats.to_dict(),
            "score": score(stats),
            "elapsed_s": round(elapsed, 1),
        }
    finally:
        restore_state(snapshot, *modules)


# --- Experiments to try (each changes ONE dimension) -----------------------

EXPERIMENTS = [
    ("baseline", {}),
    ("pilot_50pct", {"PILOT_FRACTIONS": [0.50, 0.75, 1.00]}),
    ("pilot_25pct", {"PILOT_FRACTIONS": [0.25, 0.60, 1.00]}),
    ("tighter_stop_5pct", {"DEFAULT_STOP_PCT": 0.05, "MAX_STOP_PCT": 0.07}),
    ("wider_stop_7pct", {"DEFAULT_STOP_PCT": 0.07}),
    ("faster_pyramid_1pct", {"PYRAMID_TRIGGER_PCT": 0.015}),
    ("slower_pyramid_3pct", {"PYRAMID_TRIGGER_PCT": 0.03}),
    ("more_positions_8", {"MAX_POSITIONS": 8}),
    ("fewer_positions_4", {"MAX_POSITIONS": 4}),
    ("BE_at_3R", {"BREAKEVEN_R_MULTIPLE": 3.0}),
    ("BE_at_1.5R", {"BREAKEVEN_R_MULTIPLE": 1.5}),
    ("risk_1.5pct", {"RISK_PER_TRADE": 0.015}),
    ("risk_2.5pct", {"RISK_PER_TRADE": 0.025}),
    ("looser_climax_50pct", {"CLIMAX_RUN_PCT": 0.50}),
    ("tighter_trim_10pct", {"PROFIT_TAKE_TRIM_FRACTION": 0.10}),
    # Compound tests — stacking the top individual wins
    (
        "combo_A",
        {
            "RISK_PER_TRADE": 0.025,
            "PYRAMID_TRIGGER_PCT": 0.03,
            "CLIMAX_RUN_PCT": 0.50,
        },
    ),
    (
        "combo_B",
        {
            "RISK_PER_TRADE": 0.025,
            "PYRAMID_TRIGGER_PCT": 0.03,
            "CLIMAX_RUN_PCT": 0.50,
            "MAX_POSITIONS": 4,
        },
    ),
    (
        "combo_C",
        {
            "RISK_PER_TRADE": 0.025,
            "PYRAMID_TRIGGER_PCT": 0.03,
            "CLIMAX_RUN_PCT": 0.50,
            "BREAKEVEN_R_MULTIPLE": 1.5,
        },
    ),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--fees", type=float, default=5.0)
    args = ap.parse_args()

    dfs, spy = load_data(args.years)

    print(f"\nrunning {len(EXPERIMENTS)} experiments (each ~60-80s)...\n")

    results = []
    for name, overrides in EXPERIMENTS:
        print(f"  {name:<28s} ", end="", flush=True)
        r = run_experiment(name, overrides, dfs, spy, args.years, args.fees)
        s = r["stats"]
        print(
            f"CAGR {s['cagr_pct']:+5.1f}% "
            f"DD {s['max_drawdown_pct']:4.1f}% "
            f"Sharpe {s['sharpe']:.2f}  "
            f"ret {s['total_return_pct']:+.0f}%  "
            f"score {r['score']:+.2f}"
        )
        results.append(r)

    # Rank by score
    results.sort(key=lambda x: x["score"], reverse=True)

    print("\n--- ranked by composite score (CAGR + 5*Sharpe - 0.5*DD) ---")
    for r in results:
        s = r["stats"]
        print(
            f"  {r['name']:<28s} score={r['score']:+6.2f}  "
            f"CAGR={s['cagr_pct']:+.1f}%  DD={s['max_drawdown_pct']:.1f}%  "
            f"Sharpe={s['sharpe']:.2f}  ret={s['total_return_pct']:+.0f}%"
        )

    best = results[0]
    baseline = next(r for r in results if r["name"] == "baseline")

    print("\n--- best vs baseline ---")
    print(f"  best:     {best['name']} (score {best['score']:+.2f})")
    print(f"  baseline: (score {baseline['score']:+.2f})")
    improvement = best["score"] - baseline["score"]
    print(f"  improvement: {improvement:+.2f}")

    Path("state/tuning_history.json").parent.mkdir(exist_ok=True)
    Path("state/tuning_history.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nwrote state/tuning_history.json")

    if best["name"] == "baseline":
        print("baseline already optimal — no config changes to apply.")
    elif improvement <= 0.5:
        print("improvement < 0.5 — too small to commit; keeping baseline.")
    else:
        print("\nbest overrides to commit to config.py:")
        for k, v in best["overrides"].items():
            print(f"  {k} = {v!r}")


if __name__ == "__main__":
    main()
