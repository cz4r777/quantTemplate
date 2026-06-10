"""CAN SLIM fundamental filter via FMP API (v1.1).

Replaces yfinance (unreliable, ~30% missing data) with Financial Modeling Prep.
Uses imported FMP client and CAN SLIM scoring calculators from
claude-trading-skills.

Rate-limit aware: 250 FMP calls/day on free tier. Each ticker consumes ~3 calls
(quarterly income, annual income, institutional+profile). Cache TTL = 7 days so
we spread calls across the week rather than blowing budget in one day.

Config: set FMP_API (or FMP_API_KEY) in .env.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brain.canslim.earnings_calculator import calculate_quarterly_growth, score_current_earnings
from brain.canslim.growth_calculator import calculate_annual_growth, score_annual_growth
from brain.canslim.institutional_calculator import (
    calculate_institutional_sponsorship,
    score_institutional_sponsorship,
)
from brain.fmp_client import FMPClient

CACHE_FILE = Path("state/fundamentals_cache.json")
CACHE_TTL_DAYS = 7

# O'Neil minimums: C >= 60 (EPS +18-29%), A >= 60 (stable 3yr growth)
# Don't gate on I (institutional data often sparse, score noisy)
MIN_C_SCORE = 60
MIN_A_SCORE = 60


@dataclass
class FundamentalsResult:
    symbol: str
    passes: bool = False
    c_score: int = 0
    a_score: int = 0
    i_score: int = 0
    eps_growth_q_yoy: float | None = None
    eps_growth_annual: float | None = None
    roe: float | None = None
    inst_pct: float | None = None
    gates: dict[str, bool | None] = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "passes": self.passes,
            "c_score": self.c_score,
            "a_score": self.a_score,
            "i_score": self.i_score,
            "eps_growth_q_yoy": self.eps_growth_q_yoy,
            "eps_growth_annual": self.eps_growth_annual,
            "roe": self.roe,
            "inst_pct": self.inst_pct,
            "gates": self.gates,
            "details": self.details,
        }


# --- Cache ---


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict) -> None:
    from execution.safe_io import atomic_write_json

    atomic_write_json(CACHE_FILE, cache, indent=2, default=str)


def _is_fresh(entry: dict) -> bool:
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
        return (datetime.now(UTC) - dt) < timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return False


# --- FMP client (lazy singleton) ---

_client: FMPClient | None = None


def _get_client() -> FMPClient | None:
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("FMP_API") or os.getenv("FMP_API_KEY")
    if not api_key:
        return None
    try:
        _client = FMPClient(api_key=api_key)
        return _client
    except Exception:
        return None


def _fetch_quarterly_income(client: FMPClient, symbol: str) -> list[dict]:
    try:
        data = client._rate_limited_get(
            f"{client.BASE_URL}/income-statement/{symbol}",
            params={"period": "quarter", "limit": 8},
            quiet=True,
        )
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fetch_annual_income(client: FMPClient, symbol: str) -> list[dict]:
    try:
        data = client._rate_limited_get(
            f"{client.BASE_URL}/income-statement/{symbol}",
            params={"period": "annual", "limit": 5},
            quiet=True,
        )
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fetch_institutional(client: FMPClient, symbol: str) -> tuple[list[dict], dict | None]:
    try:
        holders: list[dict] | object = (
            client._rate_limited_get(
                f"{client.BASE_URL}/institutional-holder/{symbol}",
                quiet=True,
            )
            or []
        )
        profile = client._rate_limited_get(
            f"{client.BASE_URL}/profile/{symbol}",
            quiet=True,
        )
        profile_row = profile[0] if isinstance(profile, list) and profile else None
        return (holders if isinstance(holders, list) else []), profile_row
    except Exception:
        return [], None


# --- Public API ---


def can_slim_check(symbol: str, use_cache: bool = True) -> FundamentalsResult:
    """Run CAN SLIM C + A + I checks on `symbol`. Cached for 7 days."""
    if use_cache:
        cache = _load_cache()
        entry = cache.get(symbol)
        if entry and _is_fresh(entry):
            r = FundamentalsResult(symbol=symbol)
            r.passes = entry.get("passes", False)
            r.c_score = entry.get("c_score", 0)
            r.a_score = entry.get("a_score", 0)
            r.i_score = entry.get("i_score", 0)
            r.eps_growth_q_yoy = entry.get("eps_growth_q_yoy")
            r.eps_growth_annual = entry.get("eps_growth_annual")
            r.roe = entry.get("roe")
            r.inst_pct = entry.get("inst_pct")
            r.gates = entry.get("gates", {})
            r.details = entry.get("details", {})
            return r

    r = FundamentalsResult(symbol=symbol)
    client = _get_client()
    if client is None:
        r.details["error"] = "FMP_API key not set — fundamentals disabled"
        return r

    quarterly = _fetch_quarterly_income(client, symbol)
    annual = _fetch_annual_income(client, symbol)
    holders, profile = _fetch_institutional(client, symbol)

    c_data = calculate_quarterly_growth(quarterly)
    r.eps_growth_q_yoy = c_data.get("latest_qtr_eps_growth")
    r.c_score = score_current_earnings(
        c_data.get("latest_qtr_eps_growth", 0),
        c_data.get("latest_qtr_revenue_growth", 0),
    )

    a_data = calculate_annual_growth(annual)
    r.eps_growth_annual = a_data.get("eps_cagr")
    r.a_score = score_annual_growth(
        a_data.get("eps_cagr", 0),
        a_data.get("revenue_cagr", 0),
        a_data.get("stable", False),
    )

    try:
        i_data = calculate_institutional_sponsorship(holders, profile, None)
        ownership_pct = i_data.get("ownership_pct")
        r.inst_pct = float(ownership_pct) if isinstance(ownership_pct, int | float) else None
        roe_value = None if profile is None else profile.get("returnOnEquity")
        r.roe = float(roe_value) if isinstance(roe_value, int | float) else None
        r.i_score = score_institutional_sponsorship(
            int(i_data.get("num_holders", 0) or 0),
            r.inst_pct,
            bool(i_data.get("superinvestor_present", False)),
            i_data.get("quality_warning")
            if isinstance(i_data.get("quality_warning"), str)
            else None,
        )
    except Exception:
        r.i_score = 0
        i_data = {}

    r.gates = {
        "c_score_ge_60": r.c_score >= MIN_C_SCORE,
        "a_score_ge_60": r.a_score >= MIN_A_SCORE,
        "roe_ge_15pct": (r.roe >= 0.15 if r.roe is not None else None),
        "inst_pct_ge_40pct": (r.inst_pct >= 40.0 if r.inst_pct is not None else None),
    }

    r.details = {
        "c": {
            "eps_growth": c_data.get("latest_qtr_eps_growth"),
            "rev_growth": c_data.get("latest_qtr_revenue_growth"),
        },
        "a": {
            "eps_cagr": a_data.get("eps_cagr"),
            "rev_cagr": a_data.get("revenue_cagr"),
            "stable": a_data.get("stable"),
        },
        "i": {"holders": i_data.get("num_holders"), "ownership_pct": i_data.get("ownership_pct")},
    }

    r.passes = r.c_score >= MIN_C_SCORE and r.a_score >= MIN_A_SCORE

    if use_cache:
        cache = _load_cache()
        cache[symbol] = {
            **r.to_dict(),
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        _save_cache(cache)
    return r


def warm_cache(symbols: list[str], force: bool = False) -> dict[str, FundamentalsResult]:
    """Pre-populate cache. 250 FMP calls/day ÷ 3 per ticker ≈ 80 tickers/day max."""
    return {sym: can_slim_check(sym, use_cache=not force) for sym in symbols}
