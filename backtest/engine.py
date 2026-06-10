"""Historical replay engine.

Walks the trading calendar day by day. At each day:
  1. Slice every OHLCV to include only bars up to that date.
  2. Run Gate 1 (HMM + market timing) on SPY.
  3. Update existing positions (stops, exits, pyramid).
  4. If regime allows, scan for new entries through Gate 2 + Gate 3.
  5. Record equity + trade events.

Reuses brain/ and execution/ modules unchanged — they take dataframes and
dicts, not a broker. Broker interaction is via MockBroker.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd
from brain.gate import evaluate as evaluate_gate
from brain.regime import classify_with_state as deterministic_classify_with_state

from allocation.position_sizer import full_position_shares
from backtest.broker_mock import MockBroker
from brain.breakout import detect as detect_breakout
from brain.fundamentals import can_slim_check
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
    APPLY_FUNDAMENTALS,
    HMM_STATES,
    MAG7,
    MARKET_GATE_MODE,
    MAX_POSITIONS,
    PILOT_FRACTIONS,
    REGIME_ALLOWED_FOR_ENTRY,
)
from execution import exits as exit_rules
from execution import position_manager as pm_module
from safety.portfolio_heat import can_add_risk

MAG7_SET = set(MAG7)


@dataclass
class BacktestResult:
    equity_curve: list[dict] = field(default_factory=list)  # [{date, equity, cash, positions}]
    trades: list[dict] = field(default_factory=list)  # full broker trade log
    events: list[dict] = field(default_factory=list)  # entry/exit/trim/pyramid events
    final_equity: float = 0.0
    starting_equity: float = 0.0


def _slice_dfs(dfs: dict[str, pd.DataFrame], cutoff: pd.Timestamp) -> dict[str, pd.DataFrame]:
    out = {}
    for sym, df in dfs.items():
        sliced = df.loc[df.index <= cutoff]
        if len(sliced) >= 50:
            out[sym] = sliced
    return out


def _manage_existing(
    positions: dict,
    dfs: dict[str, pd.DataFrame],
    broker: MockBroker,
    events: list,
    date_str: str,
    capital_multiplier: float = 1.0,
) -> None:
    for sym in list(positions.keys()):
        pos = positions[sym]
        df = dfs.get(sym)
        if df is None or len(df) < 50:
            continue
        price = float(df["Close"].iloc[-1])

        # Update peak before any exit decision so exit snapshot captures it
        pos["peak"] = max(pos.get("peak", price), price)

        def _exit_evt(
            reason_code: str, *, _sym: str = sym, _price: float = price, _pos: dict = pos
        ) -> dict:
            return {
                "date": date_str,
                "symbol": _sym,
                "action": "exit",
                "reason": reason_code,
                "price": _price,
                "entry_price": _pos.get("entry"),
                "entry_date": _pos.get("entry_date"),
                "peak_price": _pos.get("peak", _price),
                "shares": _pos.get("shares"),
                "layer": _pos.get("layer"),
            }

        # Hard stop
        reason = pm_module.check_exit(pos, price)
        if reason:
            broker.rebalance(sym, 0)
            events.append(_exit_evt(reason))
            pm_module.close(sym, positions)
            continue

        # Multi-rule exits
        exit_rules.mark_fast_runner(pos, df)
        sig = exit_rules.evaluate(pos, df)
        if sig is not None:
            if sig.action == "exit":
                broker.rebalance(sym, 0)
                events.append(_exit_evt(sig.reason))
                pm_module.close(sym, positions)
                continue
            elif sig.action == "trim":
                new_total = pos["shares"] - sig.trim_shares
                broker.rebalance(sym, new_total)
                pos["shares"] = new_total
                pos["partial_taken_20pct"] = True
                events.append(
                    {
                        "date": date_str,
                        "symbol": sym,
                        "action": "trim",
                        "reason": sig.reason,
                        "shares": sig.trim_shares,
                    }
                )

        # Break-even move at 2R + MA trailing stop (no partial sell anymore)
        pm_module.update_stop(pos, df)

        # Pyramid — use effective capital (equity × leverage) for sizing
        if pm_module.should_advance_layer(pos, price):
            stop = pos["stop"]
            full = full_position_shares(broker.equity() * capital_multiplier, price, stop)
            target_total = int(full * PILOT_FRACTIONS[pos["layer"] + 1])
            add = max(target_total - pos["shares"], 0)
            if add > 0:
                rec = broker.rebalance(sym, pos["shares"] + add)
                if rec.get("status") == "submitted":
                    pm_module.advance_layer(pos, price, add)
                    events.append(
                        {
                            "date": date_str,
                            "symbol": sym,
                            "action": "pyramid",
                            "new_layer": pos["layer"],
                            "shares_added": add,
                        }
                    )
                else:
                    events.append(
                        {
                            "date": date_str,
                            "symbol": sym,
                            "action": "pyramid_rejected",
                            "reason": rec.get("status"),
                            "wanted_shares": add,
                        }
                    )


def _consider_entries(
    positions: dict,
    dfs: dict[str, pd.DataFrame],
    rs_ranks: dict,
    sizing_equity: float,
    real_equity: float,
    broker: MockBroker,
    events: list,
    date_str: str,
    date: pd.Timestamp,
    sector_leaders: list[str] | None = None,
    fundamentals_pass: set[str] | None = None,
) -> None:
    """
    sizing_equity: equity * margin_multiple (for position sizing)
    real_equity: unlevered equity (for heat-cap check — ensures the 5% ceiling
        applies to real capital, not inflated leveraged capital)
    """
    open_names = set(positions.keys())
    sector_etfs = set(SECTOR_ETF.values())
    for sym, df in dfs.items():
        if sym in open_names or len(positions) >= MAX_POSITIONS:
            continue
        if sym in sector_etfs:  # don't trade sector ETFs themselves
            continue
        if len(df) < 200:
            continue
        is_mag7 = sym in MAG7_SET
        # CAN SLIM pre-filter (MAG7 bypass — they're proven)
        if fundamentals_pass is not None and not is_mag7 and sym not in fundamentals_pass:
            continue
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

        price = float(df["Close"].iloc[-1])
        base_low = float(df["Low"].tail(10).min())
        stop = pm_module.compute_initial_stop(price, base_low)
        full = full_position_shares(sizing_equity, price, stop)
        pilot_shares = int(full * PILOT_FRACTIONS[0])
        if pilot_shares <= 0:
            continue
        trade_risk = pilot_shares * (price - stop)
        ok, _ = can_add_risk(positions, real_equity, trade_risk)
        if not ok:
            continue

        rec = broker.rebalance(sym, pilot_shares)
        if rec.get("status") != "submitted":
            continue
        pm_module.open_pilot(sym, price, pilot_shares, stop, positions)
        positions[sym]["entry_date"] = date.date().isoformat()
        events.append(
            {
                "date": date_str,
                "symbol": sym,
                "action": "pilot",
                "shares": pilot_shares,
                "entry": price,
                "stop": stop,
                "breakout": bo.is_breakout,
                "pocket_pivot": bo.is_pocket_pivot,
            }
        )


def run(
    dfs: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    starting_equity: float = 100_000.0,
    commission_bps: float = 0.0,
    slippage_bps: float = 25.0,
    margin_multiple: float = 1.0,
    start_date: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    apply_fundamentals: bool | None = None,
    gate_log_path: str | None = None,
) -> BacktestResult:
    """
    dfs: {symbol: ohlcv dataframe with DatetimeIndex}
    bench_df: SPY (or other benchmark) ohlcv dataframe
    margin_multiple: 1.0 = cash-only, 1.3 = 30% margin, 2.0 = 2x leverage (Reg T max)
    """
    broker = MockBroker(
        starting_equity=starting_equity,
        commission_bps=commission_bps,
        slippage_bps=slippage_bps,
        margin_multiple=margin_multiple,
    )
    positions: dict = {}
    result = BacktestResult(starting_equity=starting_equity)

    gate_log = None
    if gate_log_path:
        gate_log = open(gate_log_path, "w", encoding="utf-8")

    # Pre-compute CAN SLIM pass set (cached via FMP). v1.1: uses FMP, not yfinance.
    if apply_fundamentals is None:
        apply_fundamentals = APPLY_FUNDAMENTALS
    fundamentals_pass: set[str] | None = None
    if apply_fundamentals:
        fundamentals_pass = set()
        for sym in dfs.keys():
            try:
                r = can_slim_check(sym, use_cache=True)
                if r.passes:
                    fundamentals_pass.add(sym)
            except Exception:
                fundamentals_pass.add(sym)  # fetch error → don't block entry

    # Align to bench dates; need ≥200 bars of history before replaying
    dates = bench_df.index.unique().sort_values()
    if start_date:
        dates = dates[dates >= pd.Timestamp(start_date)]
    # Drop the first 200 so SMAs/HMM have warmup
    if len(dates) <= 200:
        return result
    replay_dates = dates[200:]
    # HMM is expensive; refit weekly (every 5 replay days), not every day
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
        mt = assess_market_timing(bench_slice)  # kept for per-day stats below
        if gate_log:
            gate_log.write(f"{date_str} {decision.log_line()}\n")

        # Slice each symbol's data up to today's close
        today_dfs = _slice_dfs(dfs, date)

        # Update broker's mark-to-market prices
        broker.set_prices({sym: float(df["Close"].iloc[-1]) for sym, df in today_dfs.items()})

        # Compute rs_ranks on today's sliced data
        rs_ranks = compute_ranks(
            {s: d for s, d in today_dfs.items() if s not in set(SECTOR_ETF.values())}
        )

        # Leading sectors for today
        sector_leaders = leading_sectors_from_dfs(today_dfs, bench_slice) if entries_allowed else []

        _manage_existing(
            positions,
            today_dfs,
            broker,
            result.events,
            date_str,
            capital_multiplier=margin_multiple,
        )

        if entries_allowed:
            real_equity = broker.equity()
            _consider_entries(
                positions,
                today_dfs,
                rs_ranks,
                real_equity * margin_multiple,
                real_equity,
                broker,
                result.events,
                date_str,
                date,
                sector_leaders=sector_leaders,
                fundamentals_pass=fundamentals_pass,
            )

        # Charge margin interest daily on borrowed cash (when leverage active)
        broker.apply_daily_margin_interest()

        gd = decision.summary_dict()
        result.equity_curve.append(
            {
                "date": date_str,
                "equity": broker.equity(),
                "cash": broker.cash,
                "open_positions": len(positions),
                "regime": regime,
                "dd_count": mt.distribution_days,
                "obvious_bull": gd["obvious_bull"],
                "sanity_violation": sanity_violation,
                "spy_close": gd["spy_price"],
                "gate": gate_reason,
                "entries_allowed": entries_allowed,
                "gate_inputs": gd,
            }
        )
        if progress:
            progress(i + 1, total)

    if gate_log:
        gate_log.close()
    result.trades = broker.trade_log
    result.final_equity = broker.equity()
    return result
