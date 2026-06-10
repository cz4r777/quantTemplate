"""Options-v1.2 backtest engine.

Same entry signals as the v1.2 stock engine — uses the unified `evaluate_gate()`
decision (DD-counter redesign + obvious_bull override + GateDecision audit
payload). When the gate is open, buy a call instead of shares. Uses Black-
Scholes + historical vol to simulate option prices since yfinance has no
historical option chains.

Caveats:
  - Real IV often differs from HV (especially around earnings — we blackout 10d)
  - Assumes mid-price fills with fixed slippage, no bid/ask dynamics
  - No dividend handling (call prices over-estimated for dividend-paying names)
  - Rolls are not supported yet — expiring positions are closed
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd
from allocation.option_sizer import option_contracts
from brain.gate import evaluate as evaluate_gate
from brain.options.option_selector import select_contract
from brain.regime import classify_with_state as deterministic_classify_with_state
from execution.option_manager import (
    close,
    current_option_value,
    evaluate_exit,
    open_position,
)

from brain.breakout import detect as detect_breakout
from brain.hmm_classifier import RegimeClassifier
from brain.market_timing import assess as assess_market_timing
from brain.rs_rank import compute_ranks
from brain.sector_strength import (
    SECTOR_ETF,
    is_in_leading_sector,
    leading_sectors_from_dfs,
)
from brain.stage_engine import trend_template
from config import (
    HMM_STATES,
    MAG7,
    MARKET_GATE_MODE,
    MAX_POSITIONS,
    REGIME_ALLOWED_FOR_ENTRY,
)

MAG7_SET = set(MAG7)

# Options-specific adjustments
EARNINGS_BLACKOUT_DAYS = 10  # tighter than stock's 5 (vol crush risk)
OPTION_ENTRY_SLIPPAGE = 0.02  # 2% paid on entry (mid-to-ask)
OPTION_EXIT_SLIPPAGE = 0.02  # 2% given up on exit (bid-to-mid)


@dataclass
class OptionsBacktestResult:
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)  # open + close records
    events: list[dict] = field(default_factory=list)
    starting_equity: float = 0.0
    final_equity: float = 0.0


def _slice_dfs(dfs: dict[str, pd.DataFrame], cutoff: pd.Timestamp) -> dict[str, pd.DataFrame]:
    out = {}
    for sym, df in dfs.items():
        sliced = df.loc[df.index <= cutoff]
        if len(sliced) >= 50:
            out[sym] = sliced
    return out


def _manage_options(positions: dict, dfs, events, date_str, cash_ref: list[float]):
    """Re-price open options; apply exits. cash_ref is a mutable single-item list."""
    for sym in list(positions.keys()):
        pos = positions[sym]
        df = dfs.get(sym)
        if df is None:
            continue
        signal = evaluate_exit(pos, df, date_str)
        if signal is None:
            continue

        current_value = current_option_value(pos, df, date_str)
        if signal.action == "close":
            # Sell all remaining contracts at current value minus exit slippage
            exit_px = current_value * (1 - OPTION_EXIT_SLIPPAGE)
            proceeds = signal.close_contracts * exit_px * 100
            cash_ref[0] += proceeds
            pnl = proceeds - (signal.close_contracts * pos["premium_entry"] * 100)
            events.append(
                {
                    "date": date_str,
                    "symbol": sym,
                    "action": "close",
                    "reason": signal.reason,
                    "contracts": signal.close_contracts,
                    "entry_premium": pos["premium_entry"],
                    "exit_premium": exit_px,
                    "pnl": round(pnl, 2),
                }
            )
            close(sym, positions)
        elif signal.action == "trim":
            exit_px = current_value * (1 - OPTION_EXIT_SLIPPAGE)
            proceeds = signal.close_contracts * exit_px * 100
            cash_ref[0] += proceeds
            pos["contracts"] -= signal.close_contracts
            pos["partial_2x_taken"] = True
            events.append(
                {
                    "date": date_str,
                    "symbol": sym,
                    "action": "trim",
                    "reason": signal.reason,
                    "contracts_trimmed": signal.close_contracts,
                    "contracts_remaining": pos["contracts"],
                    "exit_premium": exit_px,
                }
            )


def _consider_entries(
    positions, dfs, rs_ranks, equity, events, date_str, date, sector_leaders, cash_ref
):
    open_names = set(positions.keys())
    sector_etfs = set(SECTOR_ETF.values())
    for sym, df in dfs.items():
        if sym in open_names or len(positions) >= MAX_POSITIONS:
            continue
        if sym in sector_etfs or len(df) < 200:
            continue

        is_mag7 = sym in MAG7_SET
        tt = trend_template(df, rs_rank=rs_ranks.get(sym), symbol=sym)
        if not tt.passes:
            continue
        if sector_leaders and not is_mag7:
            in_sector, _ = is_in_leading_sector(sym, sector_leaders)
            if not in_sector:
                continue
        bo = detect_breakout(df, symbol=sym, relaxed_volume=is_mag7)
        if not (bo.is_breakout or bo.is_pocket_pivot):
            continue

        # Select option contract
        contract = select_contract(df, sym, date_str)
        if contract is None:
            continue
        entry_premium = contract.premium * (1 + OPTION_ENTRY_SLIPPAGE)
        contracts = option_contracts(equity, entry_premium)
        if contracts <= 0:
            continue
        cost = contracts * entry_premium * 100
        if cost > cash_ref[0]:
            continue

        cash_ref[0] -= cost
        contract.premium = round(entry_premium, 2)  # record the price actually "paid"
        open_position(contract, contracts, positions)
        events.append(
            {
                "date": date_str,
                "symbol": sym,
                "action": "open",
                "contracts": contracts,
                "strike": contract.strike,
                "stock_price": contract.stock_price,
                "premium": contract.premium,
                "iv": contract.iv,
                "dte": contract.dte_days,
                "cost": round(cost, 2),
            }
        )


def _mark_to_market(positions: dict, dfs, date_str) -> float:
    """Sum of current option values across open positions (for equity calc)."""
    mtm = 0.0
    for sym, pos in positions.items():
        df = dfs.get(sym)
        if df is None:
            continue
        v = current_option_value(pos, df, date_str)
        mtm += pos["contracts"] * v * 100
    return mtm


def run(
    dfs: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    starting_equity: float = 100_000.0,
    start_date: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> OptionsBacktestResult:
    positions: dict = {}
    cash_ref = [starting_equity]  # mutable so nested funcs can update
    result = OptionsBacktestResult(starting_equity=starting_equity)

    dates = bench_df.index.unique().sort_values()
    if start_date:
        dates = dates[dates >= pd.Timestamp(start_date)]
    if len(dates) <= 200:
        return result
    replay_dates = dates[200:]
    hmm_cache: dict[pd.Timestamp, str] = {}
    # Deterministic hysteresis state walks forward in memory through the loop
    # so backtest matches live behavior (live persists this in state/regime.json).
    regime_state: dict = {}
    total = len(replay_dates)

    for i, date in enumerate(replay_dates):
        date_str = date.date().isoformat()
        bench_slice = bench_df.loc[bench_df.index <= date]
        if len(bench_slice) < 200:
            continue

        # Regime — deterministic SMA-stack with hysteresis by default,
        # HMM if env-flagged. Hysteresis filters single-cycle whipsaws so
        # backtest mirrors what live actually commits day to day.
        use_hmm = os.environ.get("USE_HMM_REGIME", "").lower() in ("1", "true", "yes")
        if use_hmm:
            week_key = date.to_period("W").start_time
            regime = hmm_cache.get(week_key)
            if regime is None:
                try:
                    clf = RegimeClassifier(n_states=HMM_STATES)
                    clf.fit(bench_slice)
                    regime = clf.predict(bench_slice)
                except Exception:
                    regime = "neutral"
                hmm_cache[week_key] = regime
        else:
            try:
                regime, regime_state = deterministic_classify_with_state(bench_slice, regime_state)
            except Exception:
                regime = "neutral"

        decision = evaluate_gate(
            bench_slice,
            hmm_regime=regime,
            mode=MARKET_GATE_MODE,
            regime_allowed_for_entry=REGIME_ALLOWED_FOR_ENTRY,
        )
        entries_allowed = decision.allow_entries
        gate_reason = decision.reason
        sanity_violation = decision.sanity_violation
        assess_market_timing(bench_slice)  # kept aligned with stock engine timing path

        today_dfs = _slice_dfs(dfs, date)

        # Manage existing option positions EVERY day (exits don't need entries_allowed)
        _manage_options(positions, today_dfs, result.events, date_str, cash_ref)

        if entries_allowed:
            rs_ranks = compute_ranks(
                {s: d for s, d in today_dfs.items() if s not in set(SECTOR_ETF.values())}
            )
            sector_leaders = leading_sectors_from_dfs(today_dfs, bench_slice)
            equity_now = cash_ref[0] + _mark_to_market(positions, today_dfs, date_str)
            _consider_entries(
                positions,
                today_dfs,
                rs_ranks,
                equity_now,
                result.events,
                date_str,
                date,
                sector_leaders,
                cash_ref,
            )

        mtm = _mark_to_market(positions, today_dfs, date_str)
        equity_now = cash_ref[0] + mtm
        result.equity_curve.append(
            {
                "date": date_str,
                "equity": round(equity_now, 2),
                "cash": round(cash_ref[0], 2),
                "options_mtm": round(mtm, 2),
                "open_positions": len(positions),
                "regime": regime,
                "gate_inputs": decision.summary_dict(),
                "gate_reason": gate_reason,
                "sanity_violation": sanity_violation,
            }
        )
        if progress:
            progress(i + 1, total)

    result.final_equity = cash_ref[0] + _mark_to_market(
        positions, _slice_dfs(dfs, replay_dates[-1]), replay_dates[-1].date().isoformat()
    )
    return result
