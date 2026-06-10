"""Earnings proximity filter.

Per rules.md: "No earnings within 5 trading days" before new entries.
Minervini also cuts existing positions to pilot size before earnings reports.

yfinance exposes earnings dates via Ticker.calendar and Ticker.get_earnings_dates.
Both are stale at times. When data is unavailable, we're conservative
(block the entry) — a missed trade is cheaper than a gapped loss.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import yfinance as yf

DEFAULT_BLACKOUT_DAYS = 5


def _next_earnings_date(symbol: str) -> datetime | None:
    try:
        t = yf.Ticker(symbol)
        df = t.get_earnings_dates(limit=4)
        if df is None or df.empty:
            return None
        # Keep only future earnings dates
        now = pd.Timestamp.now(tz=df.index.tz) if df.index.tz else pd.Timestamp.utcnow()
        future = df.index[df.index >= now]
        if len(future) == 0:
            return None
        return future.min().to_pydatetime()
    except Exception:
        return None


def days_until_earnings(symbol: str) -> int | None:
    """Returns trading-day-ish distance to next earnings, or None if unknown."""
    dt = _next_earnings_date(symbol)
    if dt is None:
        return None
    now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.utcnow()
    delta = (dt - now).days
    return max(delta, 0)


def blackout(
    symbol: str, days: int = DEFAULT_BLACKOUT_DAYS, strict: bool = True
) -> tuple[bool, str]:
    """
    Returns (is_blocked, reason).

    strict=True (default): unknown earnings date → blocked (conservative).
    strict=False: unknown → allowed (accept the risk).
    """
    d = days_until_earnings(symbol)
    if d is None:
        return (True, "earnings_unknown") if strict else (False, "earnings_unknown_allowed")
    if d <= days:
        return True, f"earnings_in_{d}d"
    return False, f"earnings_in_{d}d_ok"
