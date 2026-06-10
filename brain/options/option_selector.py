"""Option contract selection — pick strike and expiry for a given stock signal.

Strategy: 10-15% OTM calls, 3-6 month expiry. Prices options via Black-Scholes
using historical volatility as an IV proxy (real IV from chain would be
better but unavailable in EOD backtests).

Strike-grid (T-OPT-STRIKEGRID2):
  The static-grid approximation in _standard_strike_increment can produce
  strikes IB does not list for a given (symbol, expiry) — e.g. it picks
  TRGP $305C at a price+12% target when IB only lists $300 and $310 for
  that expiry. The broker's qualifyContracts gate catches those, so no
  bad orders go through, but the same signal keeps re-picking the same
  invalid strike on every cycle → Error 200 noise floor.

  Chain-truth path: callers (live trading) can install a chain_provider
  via set_chain_provider() or pass one to select_contract directly. The
  provider returns the actual listed strikes for (symbol, expiry); the
  selector then snaps its raw target to the lowest listed strike that
  is still OTM. If the provider raises, returns empty, or has no OTM
  strike for this expiry, select_contract returns None — fail closed,
  no fallback to the broken static grid (a known-bad strike is worse
  than no entry).

  Backtests / unit tests with no provider installed continue to use the
  legacy static grid via _round_call_strike — the path is unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

# A chain provider returns the sorted listed call strikes for (symbol,
# yyyymmdd_expiry). It's the selector's single touch-point to broker
# chain data; the live wiring lives outside this module.
ChainProvider = Callable[[str, str], "list[float]"]

# Strategy constants from user spec
STRIKE_PCT_OTM = 0.12  # mid of 10-15% OTM
DTE_TARGET = 120  # ~4 months (mid of 3-6 range)
HV_LOOKBACK_DAYS = 30  # historical vol used as IV proxy
RISK_FREE_RATE = 0.04  # current ~4% short rates


@dataclass
class OptionContract:
    symbol: str
    stock_price: float  # underlying price at entry
    strike: float
    dte_days: int  # days to expiration
    premium: float  # per share (multiply × 100 for contract cost)
    iv: float  # implied vol used (from HV proxy)
    entry_date: str
    expiry: str = ""  # YYYYMMDD — set by select_contract; needed by IB place_option_order


def _historical_vol(df: pd.DataFrame, lookback: int = HV_LOOKBACK_DAYS) -> float:
    """Annualized historical volatility from daily log returns."""
    if len(df) < lookback + 1:
        return 0.30  # fallback ~market average
    closes = df["Close"].tail(lookback + 1).astype(float).tolist()
    log_returns = [
        math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0
    ]
    if not log_returns:
        return 0.30
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / max(len(log_returns) - 1, 1)
    daily_vol = math.sqrt(var)
    return daily_vol * math.sqrt(252)  # annualize


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Call premium per share via Black-Scholes.
    S: spot, K: strike, T: years to expiry, r: risk-free, sigma: annual vol."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)


def _standard_strike_increment(stock_price: float) -> float:
    """Approximate common US equity option strike spacing.

    Live IB contract validation is still the source of truth, but this avoids
    impossible penny strikes such as 807.13C that create phantom orders.
    """
    if stock_price < 25:
        return 0.5
    if stock_price < 200:
        return 1.0
    if stock_price < 500:
        return 2.5
    return 5.0


def _round_call_strike(raw_strike: float, stock_price: float) -> float:
    increment = _standard_strike_increment(stock_price)
    strike = math.ceil(raw_strike / increment) * increment
    return round(strike, 2)


# ── Chain-truth strike selection (T-OPT-STRIKEGRID2) ────────────────────

_default_chain_provider: ChainProvider | None = None


def set_chain_provider(provider: ChainProvider | None) -> None:
    """Install the default chain provider used by select_contract when no
    chain_provider is passed explicitly. Pass None to clear (tests +
    backtests rely on the cleared state to keep using the static grid).
    """
    global _default_chain_provider
    _default_chain_provider = provider


def _snap_to_listed_strike(
    raw_target: float,
    symbol: str,
    expiry: str,
    chain_provider: ChainProvider,
) -> float | None:
    """Snap raw_target to the lowest listed strike >= raw_target.

    Returns None on any condition that means we can't honor the
    strategy's OTM intent with a real contract:
      - chain_provider raises
      - chain_provider returns empty / no positive strikes
      - no listed strike is >= raw_target (raw above the listed top)

    Returning None is the safe outcome — better to skip a signal than
    pick a known-invalid strike and burn another Error 200 cycle.
    """
    try:
        listed = chain_provider(symbol, expiry)
    except Exception:
        return None
    if not listed:
        return None
    strikes = sorted({float(s) for s in listed if s and float(s) > 0})
    if not strikes:
        return None
    otm = [s for s in strikes if s >= raw_target]
    if not otm:
        return None
    return round(otm[0], 2)


def _third_friday_yyyymmdd(target_dte_days: int, from_date: str | None = None) -> str:
    """Compute the YYYYMMDD third-Friday monthly expiry closest to target DTE.

    IB option contracts expire on standard monthly expiries (3rd Friday).
    Pick the month whose 3rd-Friday is closest to today + target_dte.
    """
    import datetime as _dt

    base = _dt.date.fromisoformat(from_date) if from_date else _dt.date.today()
    target = base + _dt.timedelta(days=target_dte_days)
    year, month = target.year, target.month
    first_day = _dt.date(year, month, 1)
    # Friday is weekday 4; offset to first Friday, then add 14 days for 3rd
    days_to_first_fri = (4 - first_day.weekday()) % 7
    third_fri = first_day + _dt.timedelta(days=days_to_first_fri + 14)
    return third_fri.strftime("%Y%m%d")


def select_contract(
    df: pd.DataFrame,
    symbol: str,
    entry_date: str,
    strike_pct_otm: float = STRIKE_PCT_OTM,
    dte_days: int = DTE_TARGET,
    *,
    chain_provider: ChainProvider | None = None,
) -> OptionContract | None:
    """Pick the option contract to buy at signal time.

    Uses Black-Scholes + historical vol to price a theoretical contract.

    Strike-selection path (T-OPT-STRIKEGRID2):
      - If chain_provider (or module-level set_chain_provider) is set,
        the raw OTM target is snapped to a real listed strike from IB's
        chain for the selected expiry. No listed OTM strike → None.
      - Otherwise (backtests, tests without a provider installed), the
        legacy static-grid approximation in _round_call_strike applies.
    """
    if len(df) < 31:
        return None
    stock_price = float(df["Close"].iloc[-1])
    if stock_price <= 0:
        return None
    raw_target = stock_price * (1 + strike_pct_otm)
    expiry = _third_friday_yyyymmdd(dte_days, entry_date)

    effective_provider = chain_provider if chain_provider is not None else _default_chain_provider
    if effective_provider is not None:
        strike = _snap_to_listed_strike(
            raw_target,
            symbol,
            expiry,
            effective_provider,
        )
        if strike is None:
            # Fail closed — never fall back to the static grid here,
            # since the whole reason a provider is installed is that
            # the static grid was producing IB-rejected strikes.
            return None
    else:
        strike = _round_call_strike(raw_target, stock_price)

    hv = _historical_vol(df)
    T = dte_days / 365.0
    premium = black_scholes_call(stock_price, strike, T, RISK_FREE_RATE, hv)
    if premium <= 0.01:
        return None
    return OptionContract(
        symbol=symbol,
        stock_price=stock_price,
        strike=strike,
        dte_days=dte_days,
        premium=round(premium, 2),
        iv=round(hv, 4),
        entry_date=entry_date,
        expiry=expiry,
    )


def reprice_contract(
    contract: OptionContract,
    current_stock_price: float,
    days_elapsed: int,
    current_hv: float | None = None,
) -> float:
    """Given a contract and updated state, return its current theoretical premium."""
    remaining_dte = max(contract.dte_days - days_elapsed, 0)
    T = remaining_dte / 365.0
    sigma = current_hv if current_hv is not None else contract.iv
    return round(
        black_scholes_call(current_stock_price, contract.strike, T, RISK_FREE_RATE, sigma),
        2,
    )
