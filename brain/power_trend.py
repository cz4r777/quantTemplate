"""Power Trend detection (TraderLion playbook).

A stock is in a Power Trend when ALL of:
  1. 21-DMA > 50-DMA
  2. Stock closes above 21-DMA for at least N consecutive days
  3. 50-DMA is rising
  4. Price is trending up (above 50-DMA)

In Power Trend, a leader rides the 21-DMA higher. The bot should:
  - Trail stops at 21-DMA (tighter than 50-DMA — already implemented at full layer)
  - Relax climax-exit threshold (let the trend run further)
  - Prefer to add layers (pyramid aggressively)

Per TraderLion: "If multiple leadership stocks fall below their 21-DMAs, the
market is under pressure." Useful as a market-wide signal too.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

MIN_DAYS_ABOVE_21DMA = 5  # stock must hold 21-DMA for ≥5 sessions
MA_ROC_DAYS = 10  # 50-DMA must be rising over last 10 days


@dataclass
class PowerTrendResult:
    symbol: str
    in_power_trend: bool
    days_above_21dma: int
    sma21: float
    sma50: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "in_power_trend": self.in_power_trend,
            "days_above_21dma": self.days_above_21dma,
            "sma21": round(self.sma21, 2),
            "sma50": round(self.sma50, 2),
        }


def detect(df: pd.DataFrame, symbol: str = "") -> PowerTrendResult:
    if len(df) < 60:
        return PowerTrendResult(symbol, False, 0, 0.0, 0.0)

    closes = df["Close"]
    sma21 = closes.rolling(21).mean()
    sma50 = closes.rolling(50).mean()

    # Days of consecutive closes above 21-DMA (from the end)
    above = closes > sma21
    days = 0
    for i in range(len(above) - 1, -1, -1):
        if bool(above.iloc[i]):
            days += 1
        else:
            break

    s21 = float(sma21.iloc[-1]) if not pd.isna(sma21.iloc[-1]) else 0.0
    s50 = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else 0.0

    # 50-DMA rising over last MA_ROC_DAYS
    s50_rising = False
    if len(sma50) > MA_ROC_DAYS and not pd.isna(sma50.iloc[-MA_ROC_DAYS - 1]):
        s50_rising = s50 > float(sma50.iloc[-MA_ROC_DAYS - 1])

    price = float(closes.iloc[-1])
    in_pt = s21 > s50 and days >= MIN_DAYS_ABOVE_21DMA and s50_rising and price > s50
    return PowerTrendResult(symbol, in_pt, days, s21, s50)
