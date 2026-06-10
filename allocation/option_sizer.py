"""Option position sizing — contracts from equity risk budget.

Max loss on a long call = premium paid × 100 × contracts. We cap that at
RISK_PER_TRADE of current equity. Simpler and cleaner than stock sizing
because the downside is mathematically bounded by the premium.
"""

from __future__ import annotations

from config import RISK_PER_TRADE


def option_contracts(equity: float, premium: float, risk_multiplier: float = 1.0) -> int:
    """How many contracts to buy given equity + premium per share.
    risk_multiplier scales per-trade risk (default 1.0 = baseline). Driven
    by the macro-regime co-signal: 0.5 in Contraction/Inflationary, 0.75 in
    Transitional, 1.0 in Concentration/Broadening."""
    if equity <= 0 or premium <= 0:
        return 0
    risk_dollars = equity * RISK_PER_TRADE * risk_multiplier
    cost_per_contract = premium * 100
    return int(risk_dollars // cost_per_contract)


def risk_multiplier_for_macro_regime(macro_regime: str | None) -> float:
    """Map macro regime label to per-trade risk multiplier."""
    if not macro_regime:
        return 1.0
    r = macro_regime.lower()
    if r in ("contraction", "inflationary"):
        return 0.5
    if r in ("transitional",):
        return 0.75
    return 1.0  # concentration/broadening/unknown


def trade_cost(contracts: int, premium: float) -> float:
    return contracts * premium * 100


def can_open_new(open_option_positions: int, max_positions: int = 6) -> bool:
    return open_option_positions < max_positions
