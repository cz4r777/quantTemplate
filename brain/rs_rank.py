"""Relative Strength ranking across a watchlist universe.

IBD's real RS rank is computed across thousands of stocks. Since we trade
FFTY only (~50 names), we compute a local rank: percentile of 6-month
return within the watchlist. Rank ≥ 70 ≈ top 30% of the watchlist.
"""

from __future__ import annotations

import pandas as pd

DEFAULT_LOOKBACK = 126  # ~6 months of trading days


def return_over(df: pd.DataFrame, lookback: int) -> float | None:
    if len(df) < lookback + 1:
        return None
    start = float(df["Close"].iloc[-lookback - 1])
    end = float(df["Close"].iloc[-1])
    if start <= 0:
        return None
    return (end / start) - 1.0


def compute_ranks(
    dfs: dict[str, pd.DataFrame],
    lookback: int = DEFAULT_LOOKBACK,
) -> dict[str, float]:
    """Returns {symbol: percentile_rank_0_to_100}."""
    returns: dict[str, float] = {}
    for sym, df in dfs.items():
        r = return_over(df, lookback)
        if r is not None:
            returns[sym] = r

    if not returns:
        return {}

    sorted_vals = sorted(returns.values())
    n = len(sorted_vals)
    ranks: dict[str, float] = {}
    for sym, r in returns.items():
        # percentile: fraction of values <= r
        below = sum(1 for v in sorted_vals if v <= r)
        ranks[sym] = 100.0 * below / n
    return ranks
