"""Industry group / sector relative strength.

Per rules.md: "Leaders in leading sectors." Filters entries to stocks whose
sector is in the top N by 3-month relative performance vs SPY.

Uses SPDR sector ETFs as sector proxies (free, liquid, cached by yfinance).
Per-ticker sector lookup via yfinance Ticker.info (one-time, cached).

Sector mapping:
  Tech: XLK   Financials: XLF   Healthcare: XLV   Consumer Cyclical: XLY
  Consumer Defensive: XLP  Industrials: XLI   Energy: XLE   Utilities: XLU
  Basic Materials: XLB   Real Estate: XLRE   Communication: XLC
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pandas as pd
import yfinance as yf

from brain.data_feed import fetch_ohlcv

SECTOR_ETF = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Financial": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

SECTOR_CACHE_FILE = Path("state/sector_cache.json")
LOOKBACK = 63  # ~3 months
TOP_N_SECTORS = 4  # "leading sectors" — top 4 of 11


def _load_cache() -> dict:
    if SECTOR_CACHE_FILE.exists():
        try:
            return json.loads(SECTOR_CACHE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    SECTOR_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECTOR_CACHE_FILE.write_text(json.dumps(cache, indent=2))


@cache
def sector_for(symbol: str) -> str | None:
    cache = _load_cache()
    if symbol in cache:
        return cache[symbol]
    try:
        info = yf.Ticker(symbol).info or {}
        sector = info.get("sector")
    except Exception:
        sector = None
    cache[symbol] = sector
    _save_cache(cache)
    return sector


def etf_for(symbol: str) -> str | None:
    s = sector_for(symbol)
    if not s:
        return None
    return SECTOR_ETF.get(s)


def leading_sectors(lookback: int = LOOKBACK, top_n: int = TOP_N_SECTORS) -> list[str]:
    """Return ETF tickers of the top-N sectors by recent performance vs SPY."""
    spy = fetch_ohlcv("SPY", lookback + 20)
    if len(spy) < lookback + 1:
        return list(set(SECTOR_ETF.values()))  # fallback: allow all
    spy_ret = float(spy["Close"].iloc[-1] / spy["Close"].iloc[-lookback - 1]) - 1.0

    scored = []
    for etf in set(SECTOR_ETF.values()):
        rel_strength = None
        try:
            df = fetch_ohlcv(etf, lookback + 20)
            if len(df) < lookback + 1:
                continue
            ret = float(df["Close"].iloc[-1] / df["Close"].iloc[-lookback - 1]) - 1.0
            rel_strength = ret - spy_ret
        except Exception:
            rel_strength = None
        if rel_strength is not None:
            scored.append((etf, rel_strength))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [etf for etf, _ in scored[:top_n]]


def is_in_leading_sector(symbol: str, leaders: list[str]) -> tuple[bool, str]:
    etf = etf_for(symbol)
    if etf is None:
        return True, "sector_unknown_allowed"  # don't block if we can't classify
    if etf in leaders:
        return True, f"leader:{etf}"
    return False, f"weak_sector:{etf}"


# --- Backtest-friendly variants (take pre-loaded dfs instead of fetching) ---


def leading_sectors_from_dfs(
    dfs: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame,
    lookback: int = LOOKBACK,
    top_n: int = TOP_N_SECTORS,
) -> list[str]:
    if len(bench_df) < lookback + 1:
        return list(set(SECTOR_ETF.values()))
    spy_ret = float(bench_df["Close"].iloc[-1] / bench_df["Close"].iloc[-lookback - 1]) - 1.0
    scored = []
    for etf in set(SECTOR_ETF.values()):
        df = dfs.get(etf)
        if df is None or len(df) < lookback + 1:
            continue
        ret = float(df["Close"].iloc[-1] / df["Close"].iloc[-lookback - 1]) - 1.0
        scored.append((etf, ret - spy_ret))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [etf for etf, _ in scored[:top_n]]
