"""Multi-rule exit logic — beyond simple stop-hit.

Returns an ExitSignal when any of these fire:
  - HARD_STOP         : close at/below current stop (position_manager handles this)
  - STAGE_3_DROP      : closes below 50-DMA on above-avg volume
  - CLIMAX_TOP        : parabolic run — 25%+ gain in last 15 sessions
  - EXHAUSTION_GAP    : gap up after big run, reversing intraday
  - PROFIT_TAKE_20PCT : +20–25% from entry, NOT under 8-week hold
  - PATTERN_VIOLATION : close below the original base low

Profit-taking interacts with the O'Neil 8-week hold rule:
  If gain ≥ 20% within 3 weeks of entry → HOLD at least 8 weeks, skip partial.
  Otherwise, trim 20–30% at +20%.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from brain.power_trend import detect as detect_power_trend

PROFIT_TAKE_PCT = 0.20
PROFIT_TAKE_TRIM_FRACTION = 0.15  # sell only 15% — let the rest run
EIGHTWEEK_TRIGGER_PCT = 0.20  # +20% in 3 weeks → hold rule
EIGHTWEEK_TRIGGER_DAYS = 21  # 3 weeks of trading days
EIGHTWEEK_HOLD_DAYS = 56  # 8 weeks of trading days
CLIMAX_RUN_PCT = 0.50  # 50% gain in 15 sessions — tuned looser (let winners run)
CLIMAX_RUN_PCT_POWER_TREND = 0.70  # Power Trend: hold even longer before exiting climax
CLIMAX_WINDOW = 15
STAGE3_VOL_MULT = 1.5  # heavier volume needed to exit (was 1.2 — noise)


@dataclass
class ExitSignal:
    symbol: str
    action: str  # "exit" | "trim"
    reason: str
    trim_shares: int = 0


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).split("T")[0]).date()
    except Exception:
        return None


def _trading_days_since(entry_date: date | None, df: pd.DataFrame) -> int | None:
    if entry_date is None:
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    mask = df.index.date >= entry_date
    return int(mask.sum()) if mask.any() else 0


def evaluate(pos: dict, df: pd.DataFrame) -> ExitSignal | None:
    """Run all exit rules in priority order. First match wins."""
    if len(df) < 50:
        return None
    sym = pos["symbol"]
    last = df.iloc[-1]
    price = float(last["Close"])
    vol = float(last["Volume"])
    vol_avg = float(df["Volume"].tail(50).mean()) if len(df) >= 50 else 0.0
    entry = float(pos["entry"])
    gain = (price - entry) / entry if entry > 0 else 0.0

    # Stage 3 drop: close below 50-DMA on above-avg volume
    sma50 = float(df["Close"].rolling(50).mean().iloc[-1])
    if price < sma50 and vol_avg > 0 and vol >= vol_avg * STAGE3_VOL_MULT:
        return ExitSignal(sym, "exit", f"stage3_drop:<50dma_vol{vol / vol_avg:.1f}x")

    # Climax run: Power Trend leaders get a looser threshold (hold longer)
    if len(df) >= CLIMAX_WINDOW + 1:
        win_start = float(df["Close"].iloc[-CLIMAX_WINDOW - 1])
        pt = detect_power_trend(df, symbol=sym)
        threshold = CLIMAX_RUN_PCT_POWER_TREND if pt.in_power_trend else CLIMAX_RUN_PCT
        if win_start > 0 and (price / win_start - 1.0) >= threshold:
            tag = "climax_pt" if pt.in_power_trend else "climax_run"
            return ExitSignal(sym, "exit", f"{tag}:{(price / win_start - 1) * 100:.0f}pct_15d")

    # Exhaustion gap: gap up > 3%, intraday reversal closing in bottom half
    open_ = float(last["Open"])
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else price
    high = float(last["High"])
    low = float(last["Low"])
    gap_up = (open_ / prev_close - 1.0) if prev_close > 0 else 0
    day_range = high - low
    closing_range = (price - low) / day_range if day_range > 0 else 1.0
    # Only fires if position already ≥ +15% (exhaustion implies prior run)
    if gain >= 0.15 and gap_up >= 0.03 and closing_range <= 0.4:
        return ExitSignal(sym, "exit", f"exhaustion_gap:{gap_up * 100:.1f}pct")

    # Profit-take at +20%, respecting 8-week hold rule
    if gain >= PROFIT_TAKE_PCT:
        days_held = _trading_days_since(_parse_date(pos.get("entry_date")), df)
        eightweek_lock = False
        # If the +20% gain was reached within the first 3 weeks → hold 8 weeks
        if days_held is not None:
            peak_in_first_3w = False
            if days_held <= EIGHTWEEK_TRIGGER_DAYS:
                peak_in_first_3w = True
            elif pos.get("fast_runner") is True:
                peak_in_first_3w = True
            if peak_in_first_3w and days_held < EIGHTWEEK_HOLD_DAYS:
                eightweek_lock = True

        if not eightweek_lock and not pos.get("partial_taken_20pct"):
            trim = max(int(pos.get("shares", 0) * PROFIT_TAKE_TRIM_FRACTION), 0)
            if trim > 0:
                return ExitSignal(sym, "trim", f"profit_take_20pct:{gain * 100:.0f}", trim)

    # Pattern violation: close below the original (initial) stop
    init_stop = float(pos.get("initial_stop") or 0.0)
    if init_stop > 0 and price < init_stop:
        return ExitSignal(sym, "exit", f"pattern_violation:<{init_stop:.2f}")

    return None


def mark_fast_runner(pos: dict, df: pd.DataFrame) -> None:
    """Flag a position if it hit +20% within 3 weeks — triggers 8-week hold."""
    if pos.get("fast_runner"):
        return
    entry_date = _parse_date(pos.get("entry_date"))
    days = _trading_days_since(entry_date, df) if entry_date else None
    if days is None or days > EIGHTWEEK_TRIGGER_DAYS:
        return
    entry = float(pos["entry"])
    peak = float(pos.get("peak", entry))
    if entry > 0 and (peak / entry - 1.0) >= EIGHTWEEK_TRIGGER_PCT:
        pos["fast_runner"] = True
