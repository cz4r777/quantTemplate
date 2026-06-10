"""Minervini-style risk-based position sizing.

Core formula:
    shares = (capital * RISK_PER_TRADE) / (entry - stop)

Dollar risk is constant per trade. Share count flexes inversely with stop
distance, so a tight-stop name gets more shares than a volatile one for
the same dollar risk.

Progressive exposure: each position scales pilot -> half -> full,
triggered only when the prior layer is profitable.
"""

from __future__ import annotations

from config import (
    DEFAULT_STOP_PCT,
    MAX_POSITIONS,
    MAX_STOP_PCT,
    MIN_STOP_PCT,
    PILOT_FRACTIONS,
    RISK_PER_TRADE,
)


def stop_price(entry: float, stop_pct: float | None = None) -> float:
    pct = stop_pct if stop_pct is not None else DEFAULT_STOP_PCT
    pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, pct))
    return entry * (1.0 - pct)


def full_position_shares(
    capital: float,
    entry: float,
    stop: float,
    risk_multiplier: float = 1.0,
) -> int:
    """risk_multiplier scales the per-trade dollar risk. Default 1.0 preserves
    existing behavior. Set to 0.75 in 'Transitional' macro regime, 0.5 in
    'Contraction'/'Inflationary' (see risk_multiplier_for_macro_regime)."""
    risk_dollars = capital * RISK_PER_TRADE * risk_multiplier
    distance = entry - stop
    if distance <= 0 or entry <= 0:
        return 0
    return int(risk_dollars // distance)


def risk_multiplier_for_macro_regime(macro_regime: str | None) -> float:
    """Map macro regime label (from macro-regime-detector skill) to a
    per-trade risk multiplier. Conservative when the structural regime
    suggests caution.

    Returns 1.0 for unknown / unset (no penalty without a signal)."""
    if not macro_regime:
        return 1.0
    r = macro_regime.lower()
    if r in ("contraction", "inflationary"):
        return 0.5
    if r in ("transitional",):
        return 0.75
    if r in ("concentration", "broadening"):
        return 1.0
    return 1.0  # unknown labels — don't penalize


def layer_shares(capital: float, entry: float, stop: float, layer: int) -> int:
    """layer: 0=pilot (25%), 1=half (50%), 2=full (100%)."""
    if layer < 0 or layer >= len(PILOT_FRACTIONS):
        return 0
    full = full_position_shares(capital, entry, stop)
    return int(full * PILOT_FRACTIONS[layer])


def can_open_new(open_positions: int) -> bool:
    return open_positions < MAX_POSITIONS


def currency_mismatch_reason(
    account_cash_currencies: set[str] | None,
    accepted_contract_currencies: set[str] | None,
    contract_currency: str | None,
) -> str | None:
    """Return a refusal reason if the contract currency is not fundable
    from the account's held cash AND not explicitly accepted via the
    operator override; else None (T-BOT-LIVE-CCY-GUARD-FIX1).

    Predicate replaces the earlier base-currency equality test (T-BOT-
    LIVE-ENABLEMENT1) which incorrectly refused every USD contract on
    an AUD-base IBKR account, even when USD cash was actually held.

    Allow when:
      - contract_currency is in account_cash_currencies, OR
      - contract_currency is in accepted_contract_currencies (env
        override permits operator-approved auto-FX exposure).

    Refuse when:
      - contract_currency is missing / empty / unknown.
      - account_cash_currencies is empty/None AND no override permits
        the contract currency (lookup failure + no escape hatch).
      - both sources are known, the contract currency is in neither.

    No secrets / account ids included in the refusal string.
    """
    ctr = (contract_currency or "").upper().strip()
    if not ctr:
        return "contract currency unknown; refusing entry"
    held = {(c or "").upper().strip() for c in (account_cash_currencies or ()) if c}
    accepted = {(c or "").upper().strip() for c in (accepted_contract_currencies or ()) if c}
    if ctr in held or ctr in accepted:
        return None
    if not held and not accepted:
        return (
            "account cash currencies unavailable; set "
            "ACCEPTED_CONTRACT_CURRENCIES only if operator approves "
            f"auto-FX exposure for contract currency {ctr}"
        )
    return (
        f"contract currency {ctr} not available in account cash "
        f"currencies {sorted(held) or '(none)'} and not present in "
        f"ACCEPTED_CONTRACT_CURRENCIES {sorted(accepted) or '(unset)'}"
    )
