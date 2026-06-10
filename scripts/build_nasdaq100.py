"""Fetch Nasdaq 100 constituents. Writes state/nasdaq100.json.

Primary source: slickcharts.com (maintained, scrape-friendly).
Fallback: hardcoded list (manually curated from NDX index).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
import pandas as pd

OUT = ROOT / "state" / "nasdaq100.json"

# Fallback list — current NDX 100 constituents. Refresh manually when NDX rebalances.
FALLBACK = [
    "AAPL",
    "ABNB",
    "ADBE",
    "ADI",
    "ADP",
    "ADSK",
    "AEP",
    "AMAT",
    "AMD",
    "AMGN",
    "AMZN",
    "ANSS",
    "APP",
    "ARM",
    "ASML",
    "AVGO",
    "AZN",
    "BIIB",
    "BKNG",
    "BKR",
    "CCEP",
    "CDNS",
    "CDW",
    "CEG",
    "CHTR",
    "CMCSA",
    "COST",
    "CPRT",
    "CRWD",
    "CSCO",
    "CSGP",
    "CSX",
    "CTAS",
    "CTSH",
    "DASH",
    "DDOG",
    "DXCM",
    "EA",
    "EXC",
    "FANG",
    "FAST",
    "FTNT",
    "GEHC",
    "GFS",
    "GILD",
    "GOOG",
    "GOOGL",
    "HON",
    "IDXX",
    "INTC",
    "INTU",
    "ISRG",
    "KDP",
    "KHC",
    "KLAC",
    "LIN",
    "LRCX",
    "LULU",
    "MAR",
    "MCHP",
    "MDB",
    "MDLZ",
    "MELI",
    "META",
    "MNST",
    "MRVL",
    "MSFT",
    "MU",
    "NFLX",
    "NVDA",
    "NXPI",
    "ODFL",
    "ON",
    "ORLY",
    "PANW",
    "PAYX",
    "PCAR",
    "PDD",
    "PEP",
    "PLTR",
    "PYPL",
    "QCOM",
    "REGN",
    "ROP",
    "ROST",
    "SBUX",
    "SNPS",
    "TEAM",
    "TMUS",
    "TSLA",
    "TTD",
    "TTWO",
    "TXN",
    "VRSK",
    "VRTX",
    "WBD",
    "WDAY",
    "XEL",
    "ZS",
]


def fetch_from_slickcharts() -> list[str]:
    url = "https://www.slickcharts.com/nasdaq100"
    headers = {"User-Agent": "Mozilla/5.0 (tradingbot watchlist)"}
    html = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True).text
    tables = pd.read_html(io.StringIO(html), flavor="lxml")
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        if "symbol" in cols:
            col = df.columns[cols.index("symbol")]
            tickers = df[col].astype(str).str.replace(".", "-", regex=False)
            tickers = [t for t in tickers if 1 <= len(t) <= 5 and (t.isalpha() or "-" in t)]
            if 90 <= len(tickers) <= 110:
                return sorted(set(tickers))
    raise RuntimeError("slickcharts parse failed")


def main() -> int:
    try:
        print("fetching Nasdaq 100 from slickcharts...")
        tickers = fetch_from_slickcharts()
        source = "slickcharts"
    except Exception as e:
        print(f"slickcharts failed ({e}); using hardcoded fallback")
        tickers = sorted(FALLBACK)
        source = "fallback"

    print(f"got {len(tickers)} tickers from {source}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "as_of": pd.Timestamp.now("UTC").date().isoformat(),
                "source": source,
                "tickers": tickers,
            },
            indent=2,
        )
    )
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
