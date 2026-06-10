"""Breakout detection — gate 3 from rules.md.

v1.1: adds a VCP-lite contraction gate. Before a breakout can be considered
valid, the base must show at least 2 volatility contractions, each tighter
than the prior one. This filters out wide/loose bases that v1.0 accepted.

A valid breakout requires ALL of:
  - Today's close > max(high) of the prior N sessions (base breakout)
  - Today's volume ≥ 1.4× 50-day average (institutional confirmation)
  - Today's close in upper half of day's range
  - Today's close above the 21-DMA (healthy action)
  - Base has ≥2 progressive contractions in the 60 days before breakout (VCP-lite)

Also detects TraderLion's Pocket Pivot (early-entry within a base):
  - Up day with volume > any down-day volume in past 10 sessions

Base count heuristic rejects 4th-stage+ bases (>2 = late stage).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

BREAKOUT_LOOKBACK = 20
BREAKOUT_MIN_CLEARANCE = 0.005  # close must be >= prior_high * 1.005 (0.5% margin).
# A breakout by $0.01 fails disproportionately; requires
# a decisive close above resistance (Minervini).
BREAKOUT_MAX_RANGE_PCT = 0.06  # (High - Low) / Close must be <= 6%.
# Wide-and-loose bars reverse more often than tight ones.
VOLUME_MULT = 1.4
VOLUME_MULT_RELAXED = 1.1
POCKET_PIVOT_WINDOW = 10
MAX_BASE_COUNT = 2
# VCP-lite
VCP_LOOKBACK = 60
VCP_MIN_CONTRACTIONS = 2
VCP_CONTRACTION_RATIO = 0.75  # each next contraction must be ≤75% of prior depth
VCP_MAX_FINAL_DEPTH = 0.15  # final contraction <15% = tight enough


@dataclass
class BreakoutResult:
    symbol: str
    is_breakout: bool = False
    is_pocket_pivot: bool = False
    base_count: int = 0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "is_breakout": self.is_breakout,
            "is_pocket_pivot": self.is_pocket_pivot,
            "base_count": self.base_count,
            "reasons": self.reasons or [],
        }


def _closing_range(row: pd.Series) -> float:
    rng = row["High"] - row["Low"]
    if rng <= 0:
        return 1.0
    return (row["Close"] - row["Low"]) / rng


def _count_contractions(df: pd.DataFrame, lookback: int = VCP_LOOKBACK) -> list[float]:
    """Identify progressive pullback depths in the last `lookback` bars.

    Scan for local peaks; measure the drawdown from each peak to the next trough.
    Returns a list of drawdown percentages in chronological order. A valid VCP
    has each drawdown smaller than the prior one.
    """
    if len(df) < lookback:
        return []
    closes = df["Close"].tail(lookback).astype(float).tolist()

    # Detect swing points: local max if higher than 3 bars on each side, same for min.
    swings = []  # list of (idx, "peak"|"trough", price)
    for i in range(3, len(closes) - 3):
        window = closes[i - 3 : i + 4]
        if closes[i] == max(window):
            swings.append((i, "peak", closes[i]))
        elif closes[i] == min(window):
            swings.append((i, "trough", closes[i]))

    # Alternate peak→trough→peak→trough; compute drawdowns
    drawdowns = []
    last_peak = None
    for _idx, kind, price in swings:
        if kind == "peak":
            last_peak = price
        elif kind == "trough" and last_peak and price < last_peak:
            drawdowns.append((last_peak - price) / last_peak)
            last_peak = None
    return drawdowns


def _two_day_confirm(df: pd.DataFrame, prior_high_today: float) -> bool:
    """Two-day breakout confirmation (reduces 'peak-0% failed breakout' pattern).

    True iff yesterday's close cleared the prior-to-yesterday 20-day high, AND
    today's close holds above yesterday's close, AND today's close is still
    above that resistance level. This eliminates the common single-day
    breakout-then-reverse failure.

    Entry price is a bit worse (day 2 instead of day 1), but failure rate drops.
    """
    if len(df) < BREAKOUT_LOOKBACK + 2:
        return False
    yesterday = df.iloc[-2]
    today = df.iloc[-1]
    # Prior 20-day high as of end-of-day-before-yesterday
    prior_block = df.iloc[-(BREAKOUT_LOOKBACK + 2) : -2]
    prior_high_yesterday = float(prior_block["High"].max())
    yesterday_close = float(yesterday["Close"])
    today_close = float(today["Close"])
    yesterday_cleared = yesterday_close >= prior_high_yesterday * (1 + BREAKOUT_MIN_CLEARANCE)
    today_holds = today_close >= yesterday_close * (1 - 0.005)  # 0.5% tolerance
    still_above = today_close >= prior_high_yesterday * (1 + BREAKOUT_MIN_CLEARANCE)
    return yesterday_cleared and today_holds and still_above


def _is_valid_vcp_base(df: pd.DataFrame) -> tuple[bool, str]:
    """Does the pre-breakout window show the VCP signature?

    Returns (passes, reason). False reason explains what's missing.
    """
    contractions = _count_contractions(df)
    if len(contractions) < VCP_MIN_CONTRACTIONS:
        return False, f"only_{len(contractions)}_contractions"
    # Each contraction tighter than the prior
    for i in range(1, len(contractions)):
        if contractions[i] > contractions[i - 1] * VCP_CONTRACTION_RATIO + 0.01:
            return False, f"contraction_{i}_not_tighter"
    if contractions[-1] > VCP_MAX_FINAL_DEPTH:
        return False, f"final_contraction_too_deep_{contractions[-1]:.1%}"
    return True, "vcp_ok"


def _count_bases(df: pd.DataFrame, lookback: int = 252, threshold: float = 0.15) -> int:
    """Count distinct bases in the last year.

    A new base starts each time the stock drops ≥15% from a new high and then
    recovers to make another new high. This matches O'Neil's definition of
    base-to-base progression (Stage 2 advances form sequential bases).

    Default 15% threshold avoids counting every pullback as a new base.
    """
    s = df["Close"].tail(lookback)
    if len(s) < 50:
        return 1
    bases = 1
    highwater = float(s.iloc[0])
    in_drawdown = False
    for p in s:
        p = float(p)
        if in_drawdown and p > highwater:
            bases += 1
            in_drawdown = False
            highwater = p
        elif not in_drawdown and p > highwater:
            highwater = p
        elif p <= highwater * (1 - threshold):
            in_drawdown = True
    return bases


def detect(df: pd.DataFrame, symbol: str = "", relaxed_volume: bool = False) -> BreakoutResult:
    r = BreakoutResult(symbol=symbol, reasons=[])
    if len(df) < 60:
        r.reasons.append("insufficient_history")
        return r

    today = df.iloc[-1]
    prior = df.iloc[-BREAKOUT_LOOKBACK - 1 : -1]

    vol_avg = df["Volume"].tail(50).mean()
    prior_high = prior["High"].max()
    sma21 = df["Close"].rolling(21).mean().iloc[-1]

    vol_mult = VOLUME_MULT_RELAXED if relaxed_volume else VOLUME_MULT
    bar_range_pct = (float(today["High"]) - float(today["Low"])) / max(float(today["Close"]), 1e-9)
    checks = {
        "close_clears_prior_high": (
            float(today["Close"]) >= float(prior_high) * (1 + BREAKOUT_MIN_CLEARANCE)
        ),
        f"volume_ge_{vol_mult}x_avg": float(today["Volume"]) >= float(vol_avg) * vol_mult,
        "close_in_upper_half": _closing_range(today) >= 0.5,
        "close_above_sma21": float(today["Close"]) > float(sma21),
        "tight_bar": bar_range_pct <= BREAKOUT_MAX_RANGE_PCT,
    }
    # Tested and reverted: "low_holds_prior_high" (clean-bar) and
    # "two_day_confirm" (wait one bar). Both cost more alpha than they saved
    # (-17 to -46 each). The peak-0% failures are dominated by day-2-or-later
    # reversals, which neither filter catches without also rejecting many
    # real breakouts.
    r.reasons = [k for k, v in checks.items() if not v]
    r.is_breakout = all(checks.values())

    # v1.1 NOTE: VCP-lite gate was added here and found to reject too many real
    # breakouts (cost ~10 CAGR points in 2024 bull year). Disabled by default.
    # Code preserved for v2.0 where a proper state machine replaces this.

    # Pocket Pivot (secondary signal)
    recent = df.tail(POCKET_PIVOT_WINDOW + 1).iloc[:-1]
    if len(recent) == POCKET_PIVOT_WINDOW:
        down_days = recent[recent["Close"] < recent["Open"]]
        max_down_vol = float(down_days["Volume"].max()) if len(down_days) else 0.0
        up_today = today["Close"] > today["Open"]
        r.is_pocket_pivot = bool(up_today and today["Volume"] > max_down_vol)

    # Base count (reject late-stage)
    r.base_count = _count_bases(df)
    if r.base_count > MAX_BASE_COUNT:
        r.is_breakout = False
        r.is_pocket_pivot = False
        r.reasons.append(f"late_stage_base:{r.base_count}")

    return r
