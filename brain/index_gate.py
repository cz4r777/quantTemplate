"""Index-appropriate entry gate for index-options strategies.

Replaces trend_template() for benchmark indices (SPY, QQQ, etc).

Why a separate gate exists:
  trend_template was designed by Minervini to filter INDIVIDUAL STOCKS
  by relative strength against a benchmark. Two of its gates make no
  sense for the benchmark itself:
    - Gate 8 (RS rank ≥ 70): SPY's RS rank vs the universe is ~50 by
      definition. SPY IS the benchmark — it cannot beat itself. SPY
      will essentially never pass this gate, so the bot never trades
      SPY calls.
    - Gate 4 (SMA50 > SMA150 > SMA200): SPY/QQQ stack alignment is
      sensitive to small drifts; the index doesn't always have a clean
      multi-MA stack even during clear uptrends.

The bug: options-v1.x bots have placed ZERO orders across their
lifetime because trend_template was the wrong tool for the [SPY, QQQ]
universe.

What this gate checks instead:
  1. The IBD market-timing model says we're in confirmed_uptrend
     (this is the right granularity for "is the broad market in an
     uptrend that supports long-call positions")
  2. The index ITSELF has a basic uptrend stack: last > SMA50 > SMA200
     (less strict than trend_template's 5-MA stack, sufficient for
     the benchmark)

This way SPY can trade calls when SPY's own structure is intact, and
QQQ separately when QQQ's structure is. They no longer block each
other via a misapplied per-stock filter.
"""

from __future__ import annotations

import pandas as pd

INDEX_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA"}


def is_index(symbol: str) -> bool:
    return symbol.upper() in INDEX_SYMBOLS


def index_uptrend_gate(
    symbol: str,
    df: pd.DataFrame,
    market_timing_state: str,
) -> tuple[bool, str]:
    """Returns (passes, reason).

    market_timing_state: brain.market_timing.assess(bench_df).state — one of
      'confirmed_uptrend', 'under_pressure', 'correction'. Already computed
      at the cycle level on SPY; passed in to avoid re-computing.
    """
    if market_timing_state != "confirmed_uptrend":
        return False, f"mt_{market_timing_state}"

    close = df["Close"]
    if len(close) < 200:
        return False, "insufficient_history"

    last = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])

    if last <= sma50:
        return False, f"below_sma50:{last:.2f}<={sma50:.2f}"
    if sma50 <= sma200:
        return False, f"sma50_below_sma200:{sma50:.2f}<={sma200:.2f}"

    return True, f"uptrend:last={last:.2f}>sma50={sma50:.2f}>sma200={sma200:.2f}"
