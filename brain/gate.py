"""Market-entry gate — unified decision record.

Single source of truth for "is the market OK to open new entries today?"
Used by both the backtest engine and live trading, so whatever the operator
sees on the dashboard is exactly what the bot used.

Output is a `GateDecision` with every input, the decision path, and a
human-readable explanation. `summary_dict()` returns a shape the dashboard
can render directly.

The logic here is intentionally transparent — each input is computed
separately and each branch is a single boolean, so in live trading the
operator can look at the dashboard and understand exactly why the gate is
on or off, and override if needed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from brain.market_timing import (
    assess as assess_market_timing,
)
from brain.market_timing import (
    in_obvious_bull,
    in_recovery_bull,
    minervini_price_gate,
)


@dataclass
class GateInputs:
    """Raw market state inputs the gate sees. Keep this flat — dashboard-friendly."""

    spy_price: float = 0.0
    spy_vs_21dma_pct: float = 0.0
    spy_vs_50dma_pct: float = 0.0
    spy_vs_200dma_pct: float = 0.0
    ma50_rising: bool = False
    ma200_rising: bool = False
    mt_state: str = "unknown"  # confirmed_uptrend | under_pressure | correction
    dd_count: int = 0
    hmm_regime: str = "unknown"  # bull / bull_run / neutral / bear / ...
    obvious_bull: bool = False
    recovery_bull: bool = False  # FTD-based post-bear override eligible


@dataclass
class GateDecision:
    allow_entries: bool
    reason: str  # short code for logs / alerts
    path: str  # which branch decided (primary / override / block)
    sanity_violation: bool = False  # gate would have blocked but override fired
    inputs: GateInputs = field(default_factory=GateInputs)

    def summary_dict(self) -> dict[str, Any]:
        """Dashboard-ready flat dict. Safe to JSON-serialize."""
        d = asdict(self.inputs)
        d.update(
            {
                "allow_entries": self.allow_entries,
                "reason": self.reason,
                "path": self.path,
                "sanity_violation": self.sanity_violation,
            }
        )
        return d

    def log_line(self) -> str:
        """Single human-readable line for terminal/log files."""
        i = self.inputs
        arrow = "ON " if self.allow_entries else "OFF"
        flag = " !SANITY" if self.sanity_violation else ""
        return (
            f"[GATE {arrow}] SPY ${i.spy_price:>7.2f} "
            f"21={i.spy_vs_21dma_pct:+5.1f}% 50={i.spy_vs_50dma_pct:+5.1f}% "
            f"200={i.spy_vs_200dma_pct:+5.1f}%  50r={'Y' if i.ma50_rising else 'N'}  "
            f"DD={i.dd_count} mt={i.mt_state:17s} hmm={i.hmm_regime:9s}  "
            f"path={self.path}  reason={self.reason}{flag}"
        )

    def explain(self) -> list[str]:
        """Multi-line explanation for dashboard tooltips / debug mode."""
        i = self.inputs
        lines = [
            f"Market entry gate: {'OPEN' if self.allow_entries else 'CLOSED'}",
            f"  SPY close: ${i.spy_price:.2f}",
            f"  vs 21-DMA: {i.spy_vs_21dma_pct:+.2f}%  "
            f"vs 50-DMA: {i.spy_vs_50dma_pct:+.2f}%  vs 200-DMA: {i.spy_vs_200dma_pct:+.2f}%",
            f"  50-DMA rising: {i.ma50_rising}    200-DMA rising: {i.ma200_rising}",
            f"  Market timing: {i.mt_state} (distribution days: {i.dd_count})",
            f"  HMM regime: {i.hmm_regime}",
            f"  Obvious-bull override eligible: {i.obvious_bull}",
            f"  Decision path: {self.path}",
            f"  Reason: {self.reason}",
        ]
        if self.sanity_violation:
            lines.append("  !! SANITY VIOLATION: primary gate blocked but override kept us in.")
        return lines


def _compute_inputs(bench_df: pd.DataFrame, hmm_regime: str) -> GateInputs:
    close = bench_df["Close"]
    price = float(close.iloc[-1])

    def ma(n: int, offset: int = 0) -> float:
        tail = close.iloc[-(n + offset) : -offset] if offset else close.tail(n)
        return float(tail.mean()) if len(tail) >= n else 0.0

    ma21 = ma(21)
    ma50_today = ma(50)
    ma50_prior = ma(50, offset=1)
    ma200_today = ma(200)
    ma200_prior = ma(200, offset=1)

    mt = assess_market_timing(bench_df)
    ob = in_obvious_bull(bench_df)
    rb = in_recovery_bull(bench_df)

    return GateInputs(
        spy_price=price,
        spy_vs_21dma_pct=((price / ma21 - 1) * 100) if ma21 > 0 else 0.0,
        spy_vs_50dma_pct=((price / ma50_today - 1) * 100) if ma50_today > 0 else 0.0,
        spy_vs_200dma_pct=((price / ma200_today - 1) * 100) if ma200_today > 0 else 0.0,
        ma50_rising=ma50_today > ma50_prior,
        ma200_rising=ma200_today > ma200_prior,
        mt_state=mt.state,
        dd_count=mt.distribution_days,
        hmm_regime=hmm_regime,
        obvious_bull=ob,
        recovery_bull=rb,
    )


def evaluate(
    bench_df: pd.DataFrame,
    hmm_regime: str,
    mode: str = "hmm+dd",
    regime_allowed_for_entry: set[str] | None = None,
) -> GateDecision:
    """
    Evaluate the market-entry gate for today.

    mode:
      "hmm+dd" : HMM regime + tightened DD counter + obvious-bull override
      "price"  : pure Minervini price-trend check (21/50/200 MA stack + rising)

    Returns a GateDecision the engine + dashboard can consume identically.
    """
    inputs = _compute_inputs(bench_df, hmm_regime)

    if mode == "price":
        allowed, reason = minervini_price_gate(bench_df)
        return GateDecision(
            allow_entries=allowed,
            reason=reason,
            path="price_mode",
            inputs=inputs,
        )

    allowed_regimes = regime_allowed_for_entry or set()
    primary_allowed = (
        inputs.hmm_regime in allowed_regimes and inputs.mt_state == "confirmed_uptrend"
    )

    if primary_allowed:
        return GateDecision(
            allow_entries=True,
            reason=f"{inputs.hmm_regime}/{inputs.mt_state}",
            path="primary_open",
            inputs=inputs,
        )

    # Override: obvious bull (SPY above rising 200-DMA). Correction-level
    # DD is the only thing allowed to block this, but "correction" is itself
    # gated on SPY < 200-DMA inside market_timing.assess — so in practice
    # when obvious_bull=True, mt_state is at most "under_pressure".
    #
    # An FTD-based "recovery_bull" override was tried and rolled back: in
    # 2023-W12 and 2026-W14 it never fired because SPY was still below the
    # 50-DMA during the choppy recovery chop. In the weeks where it DID
    # fire, the extra entries netted negative, so it was added noise.
    # Trade count went from 585 -> 604 with no alpha. Reverted.
    if inputs.obvious_bull and inputs.mt_state != "correction":
        return GateDecision(
            allow_entries=True,
            reason=f"bull_override({inputs.hmm_regime}/{inputs.mt_state})",
            path="override_fired",
            sanity_violation=True,
            inputs=inputs,
        )

    return GateDecision(
        allow_entries=False,
        reason=f"{inputs.hmm_regime}/{inputs.mt_state}",
        path="blocked",
        inputs=inputs,
    )
