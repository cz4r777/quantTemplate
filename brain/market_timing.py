"""Market-timing layer from playbook/market_timing.md.

Two IBD signals that complement the HMM regime:

  Distribution Day (DD):
    Index closes ≥ 0.2% lower than prior day AND volume > prior day volume.
    Counted over a rolling 25 trading sessions. A day expires when:
      - 25 sessions have passed, OR
      - index gains ≥ 5% from its close on that day.

  Follow-Through Day (FTD):
    During a downtrend/rally-attempt, index closes ≥ 1.25% higher than prior day
    on volume > prior day. Typically day 4+ of the attempt is the tradable one.
    FTD flips the regime from Correction → Confirmed Uptrend.

Thresholds:
  0-2 DDs = healthy uptrend
  3     = monitor (uptrend at risk)
  4-5   = "Under Pressure" — halt new entries
  6+    = correction likely — defensive
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

DD_PCT_THRESHOLD = -0.005  # -0.5% (was -0.2% — too sensitive, fired
# on every routine down day in grinding bulls)
DD_WINDOW = 25  # rolling trading sessions
DD_EXPIRY_GAIN = 0.04  # +4% from DD close expires (was 5% —
# too demanding, DDs stacked faster than they cleared)
DD_VOLUME_VS_AVG = 1.00  # volume must exceed 20-day average, not just
# prior-day. Original O'Neil rule "volume > prior day"
# breaks in years of secular rising volume (e.g. 2021
# retail wave) where every day has volume > prior.
DD_VOLUME_AVG_WINDOW = 20
FTD_PCT_THRESHOLD = 0.0125  # +1.25%

DD_HEALTHY = 2
DD_UNDER_PRESSURE = 6  # was 4 — IBD publishes 4 as "watch" and 6 as
# "act/halt"; using 4 blocked us out of ~50% of
# 2021 and 2022-H2 (drilldown avg DD 5-6 in
# under-performing windows). Minervini's books
# reference 6+ as the threshold for real concern.
DD_CORRECTION = 8  # was 6 — matches the 2-level shift


@dataclass
class MarketTimingResult:
    distribution_days: int
    is_ftd_today: bool
    state: str  # confirmed_uptrend | under_pressure | correction
    notes: list[str]

    def to_dict(self) -> dict:
        return {
            "distribution_days": self.distribution_days,
            "is_ftd_today": self.is_ftd_today,
            "state": self.state,
            "notes": self.notes,
        }


def _daily_returns(df: pd.DataFrame) -> pd.Series:
    return df["Close"].pct_change()


def count_distribution_days(df: pd.DataFrame, window: int = DD_WINDOW) -> int:
    """Count valid (non-expired) distribution days in the last `window` sessions.

    v1.2: tightened both legs of the "distribution" definition so the counter
    doesn't trip on routine down days in a grinding bull:
      * decline must be >= 0.5% (was 0.2%)
      * volume must exceed the 20-day average (was: just > prior day's volume,
        which always passed in years of secular rising volume)
    """
    need = max(window + 1, DD_VOLUME_AVG_WINDOW + 1)
    if len(df) < need:
        return 0
    tail = df.tail(window + 1).copy()
    returns = tail["Close"].pct_change()

    # Volume reference: rolling avg of prior DD_VOLUME_AVG_WINDOW bars (exclusive of today)
    vol_avg = df["Volume"].rolling(DD_VOLUME_AVG_WINDOW).mean().shift(1)
    vol_avg_tail = vol_avg.loc[tail.index]
    vol_ratio = tail["Volume"] / vol_avg_tail

    candidates = (returns <= DD_PCT_THRESHOLD) & (vol_ratio >= DD_VOLUME_VS_AVG)

    # Expire DDs where index has since gained >= DD_EXPIRY_GAIN from that close
    final_close = float(df["Close"].iloc[-1])
    valid = 0
    for i in range(1, len(tail)):
        if not bool(candidates.iloc[i]):
            continue
        dd_close = float(tail["Close"].iloc[i])
        if dd_close <= 0:
            continue
        gain_since = (final_close - dd_close) / dd_close
        if gain_since >= DD_EXPIRY_GAIN:
            continue
        valid += 1
    return valid


def is_follow_through_day(df: pd.DataFrame) -> bool:
    """Today's bar shows FTD characteristics: >=1.25% up on higher volume."""
    if len(df) < 2:
        return False
    today = df.iloc[-1]
    yesterday = df.iloc[-2]
    ret = (float(today["Close"]) / float(yesterday["Close"])) - 1.0
    vol_up = float(today["Volume"]) > float(yesterday["Volume"])
    return ret >= FTD_PCT_THRESHOLD and vol_up


def recent_ftd(df: pd.DataFrame, lookback: int = 10) -> bool:
    """Has an FTD (>=1.25% up on higher volume) fired in the last `lookback` bars?"""
    if len(df) < lookback + 1:
        return False
    tail = df.tail(lookback + 1)
    returns = tail["Close"].pct_change()
    vol_up = tail["Volume"].diff() > 0
    ftd_mask = (returns >= FTD_PCT_THRESHOLD) & vol_up
    return bool(ftd_mask.iloc[1:].any())


def in_recovery_bull(df: pd.DataFrame, ftd_lookback: int = 10) -> bool:
    """Post-bear 'recovery' override: FTD fired recently AND SPY > rising 50-DMA.

    Used as a secondary fail-safe for the early months of a new bull, before
    SPY/200-DMA has had time to turn up. Catches:
      * 2023-W12 (Mar 2023 bottom): SPY above 200-DMA but 200-DMA still
        falling from 2022 bear; 50-DMA was rising; FTD had fired
      * 2025-W18 / 2026-W14 (tariff-shock recoveries)

    Stricter than `in_obvious_bull` because 50-DMA can chop — we require an
    explicit FTD event as the trigger (O'Neil's own recovery entry rule).
    """
    if len(df) < 51:
        return False
    close = df["Close"]
    price = float(close.iloc[-1])
    ma50_today = float(close.tail(50).mean())
    ma50_prior = float(close.iloc[-51:-1].mean())
    if not (price > ma50_today and ma50_today > ma50_prior):
        return False
    return recent_ftd(df, lookback=ftd_lookback)


def _in_price_uptrend(df: pd.DataFrame) -> bool:
    """Minervini-style price-action check: SPY > 21-DMA AND 21-DMA rising."""
    if len(df) < 25:
        return False
    close = df["Close"]
    ma21_today = float(close.tail(21).mean())
    ma21_prior = float(close.iloc[-22:-1].mean())
    price = float(close.iloc[-1])
    return price > ma21_today and ma21_today > ma21_prior


def in_obvious_bull(df: pd.DataFrame) -> bool:
    """'Obvious bull' safety override: SPY above a rising 200-DMA.

    This is the market-wide long-term trend signal. When this holds the market
    is in a durable uptrend regardless of what the DD counter or HMM say, so
    the DD gate should not lock us out. When it does, we log a SANITY
    violation (see engine) so the operator is flagged.

    v1.2: was previously `SPY > 200-DMA AND price > 50-DMA AND 50-DMA rising`
    but that failed on 2021-W40 (SPY +5% above 200-DMA, but 50-DMA had gone
    flat mid-October). The 50-DMA is too noisy to act as a trend gate — the
    200-DMA is the durable signal Minervini uses.
    """
    if len(df) < 201:
        return False
    close = df["Close"]
    price = float(close.iloc[-1])
    ma200_today = float(close.tail(200).mean())
    ma200_prior = float(close.iloc[-201:-1].mean())
    return price > ma200_today and ma200_today > ma200_prior


def minervini_price_gate(df: pd.DataFrame) -> tuple[bool, str]:
    """Market-entry gate based purely on SPY price-trend (Minervini Power-Trend style).

    Returns (entries_allowed, reason). True iff:
      * SPY Close > 21-DMA
      * 21-DMA > 50-DMA > 200-DMA
      * 21-DMA rising (today > yesterday)

    No HMM, no distribution-day counter — both proved fragile (see config note).
    """
    if len(df) < 200:
        return False, "insufficient_history"
    close = df["Close"]
    price = float(close.iloc[-1])
    ma21_today = float(close.tail(21).mean())
    ma21_prior = float(close.iloc[-22:-1].mean())
    ma50 = float(close.tail(50).mean())
    ma200 = float(close.tail(200).mean())

    if price <= ma21_today:
        return False, f"price_below_21dma:{price:.2f}<{ma21_today:.2f}"
    if ma21_today <= ma50:
        return False, f"21dma_below_50dma:{ma21_today:.2f}<{ma50:.2f}"
    if ma50 <= ma200:
        return False, f"50dma_below_200dma:{ma50:.2f}<{ma200:.2f}"
    if ma21_today <= ma21_prior:
        return False, f"21dma_falling:{ma21_today:.2f}<={ma21_prior:.2f}"
    return True, "price_uptrend"


def _spy_below_200dma(df: pd.DataFrame) -> bool:
    if len(df) < 200:
        return False
    ma200 = float(df["Close"].tail(200).mean())
    return float(df["Close"].iloc[-1]) < ma200


def assess(df: pd.DataFrame) -> MarketTimingResult:
    dd = count_distribution_days(df)
    ftd = is_follow_through_day(df)
    notes: list[str] = []

    # v1.2: "correction" requires BOTH a high DD count AND SPY below 200-DMA.
    # Prior behavior let DD count alone force correction state — which fired
    # in 2024-W32 when SPY was +4% above 200-DMA with DD=10 (volatility noise,
    # not a real correction). Real corrections always include SPY below its
    # 200-DMA; if that isn't true, at worst the market is "under_pressure".
    below_200 = _spy_below_200dma(df)
    if dd >= DD_CORRECTION and below_200:
        state = "correction"
        notes.append(f"distribution_count:{dd}_correction_likely")
    elif dd >= DD_UNDER_PRESSURE:
        state = "under_pressure"
        if dd >= DD_CORRECTION and not below_200:
            notes.append(f"distribution_count:{dd}_above_200dma_downgraded_to_pressure")
        else:
            notes.append(f"distribution_count:{dd}_under_pressure")
    else:
        state = "confirmed_uptrend"
        if dd > DD_HEALTHY:
            notes.append(f"distribution_count:{dd}_monitor")

    if ftd:
        notes.append("follow_through_day")

    return MarketTimingResult(
        distribution_days=dd,
        is_ftd_today=ftd,
        state=state,
        notes=notes,
    )
