"""Fetch S&P 500 constituents, compute 6-month relative strength, keep top 50.

Writes state/sp500_top50.json — consumed by build_watchlist.py to merge
into the main watchlist. Refresh weekly via cron.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import io

import httpx
import pandas as pd
import yfinance as yf

OUT = ROOT / "state" / "sp500_top50.json"
LOOKBACK = "6mo"


def fetch_sp500_tickers() -> list[str]:
    """Pull constituents from the maintained `datasets/s-and-p-500-companies` CSV."""
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
    headers = {"User-Agent": "Mozilla/5.0 (tradingbot watchlist builder)"}
    csv_text = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True).text
    df = pd.read_csv(io.StringIO(csv_text))
    # yfinance uses '-' for class shares where Wikipedia uses '.' (e.g. BRK.B -> BRK-B)
    tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    return sorted(set(tickers))


def six_month_returns(tickers: list[str]) -> dict[str, float]:
    """Batch-download 6mo history and return {ticker: pct_return}."""
    data = yf.download(
        tickers,
        period=LOOKBACK,
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )
    out: dict[str, float] = {}
    if isinstance(data.columns, pd.MultiIndex):
        for t in tickers:
            try:
                closes = data[t]["Close"].dropna()
                if len(closes) < 60:
                    continue
                out[t] = float(closes.iloc[-1] / closes.iloc[0] - 1.0)
            except (KeyError, IndexError):
                continue
    return out


def main() -> int:
    print("fetching S&P 500 constituents...")
    tickers = fetch_sp500_tickers()
    print(f"got {len(tickers)} tickers. pulling 6mo history (batched)...")

    t0 = time.time()
    returns = six_month_returns(tickers)
    print(f"returns computed in {time.time() - t0:.1f}s for {len(returns)} names")

    ranked = sorted(returns.items(), key=lambda x: x[1], reverse=True)
    top50 = [t for t, _ in ranked[:50]]
    top50_with_ret = [(t, r) for t, r in ranked[:50]]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "as_of": pd.Timestamp.now("UTC").date().isoformat(),
                "lookback": LOOKBACK,
                "tickers": top50,
                "returns": {t: round(r * 100, 2) for t, r in top50_with_ret},
            },
            indent=2,
        )
    )

    print(f"top 50 by 6mo RS -> {OUT}")
    print(f"strongest: {top50[0]} ({top50_with_ret[0][1] * 100:+.1f}%)")
    print(f"weakest (of top 50): {top50[-1]} ({top50_with_ret[-1][1] * 100:+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
