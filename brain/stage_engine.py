"""Minervini Trend Template — encodes the 8 gates from playbook/rules.md.

All 8 must pass for a stock to qualify as Stage 2.
Uses simple moving averages (not EMA), per Minervini's spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class TrendTemplateResult:
    symbol: str
    passes: bool
    gates: dict[str, bool] = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "passes": self.passes,
            "gates": self.gates,
            "details": self.details,
        }


def _compute_smas(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["Close"]
    out["sma50"] = close.rolling(50).mean()
    out["sma150"] = close.rolling(150).mean()
    out["sma200"] = close.rolling(200).mean()
    return out


def _sma200_trend_up(df: pd.DataFrame, days: int = 22) -> bool:
    """200-day SMA trending up for at least `days` sessions (~1 month)."""
    s = df["sma200"]
    if len(s) < days + 1 or s.iloc[-1] != s.iloc[-1]:
        return False
    return bool(s.iloc[-1] > s.iloc[-days])


def trend_template(
    df: pd.DataFrame,
    rs_rank: float | None = None,
    symbol: str = "",
) -> TrendTemplateResult:
    """Run all 8 Minervini gates on a daily OHLCV dataframe.

    rs_rank: 0-100 percentile across the watchlist universe (compute separately).
             If None, gate 8 is skipped (marked None in results).
    """
    r = TrendTemplateResult(symbol=symbol, passes=False)

    if len(df) < 200:
        r.details["error"] = f"need 200 bars, have {len(df)}"
        return r

    df = _compute_smas(df)
    last = df.iloc[-1]
    price = float(last["Close"])
    sma50 = float(last["sma50"])
    sma150 = float(last["sma150"])
    sma200 = float(last["sma200"])

    window_52w = df.tail(252) if len(df) >= 252 else df
    high_52w = float(window_52w["High"].max())
    low_52w = float(window_52w["Low"].min())

    r.gates["1_price_above_150_and_200"] = price > sma150 and price > sma200
    r.gates["2_sma150_above_sma200"] = sma150 > sma200
    r.gates["3_sma200_trending_up"] = _sma200_trend_up(df, days=22)
    r.gates["4_sma50_above_150_and_200"] = sma50 > sma150 and sma50 > sma200
    r.gates["5_price_above_sma50"] = price > sma50
    r.gates["6_price_25pct_above_52w_low"] = price >= low_52w * 1.25
    r.gates["7_price_within_25pct_of_52w_high"] = price >= high_52w * 0.75

    if rs_rank is not None:
        r.gates["8_rs_rank_ge_70"] = rs_rank >= 70.0  # v1.2 tested 80: cost
        # 110 alpha (too few
        # trades); win rate did
        # NOT improve. Reverted.
    else:
        r.gates["8_rs_rank_ge_70"] = False

    r.passes = all(r.gates.values())
    r.details = {
        "price": price,
        "sma50": sma50,
        "sma150": sma150,
        "sma200": sma200,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "rs_rank": rs_rank,
    }
    return r


def classify_stage(df: pd.DataFrame) -> int:
    """Weinstein 4-stage rough classifier. Use trend_template for the hard filter."""
    if len(df) < 200:
        return 0
    df = _compute_smas(df)
    last = df.iloc[-1]
    price = float(last["Close"])
    sma150 = float(last["sma150"])
    sma200 = float(last["sma200"])
    trending_up = _sma200_trend_up(df, days=22)
    trending_down = df["sma200"].iloc[-1] < df["sma200"].iloc[-22]

    if price > sma150 > sma200 and trending_up:
        return 2  # advancing
    if price < sma150 and price < sma200 and trending_down:
        return 4  # declining
    if price > sma200 and not trending_up:
        return 3  # topping
    return 1  # basing
