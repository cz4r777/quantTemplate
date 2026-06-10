"""Phantom-position safety guard.

Confirms an option position recorded in our state file is actually held
at the IB broker. If state and broker disagree (e.g., bot wrote the
record but the order never filled, or state is stale across a restart),
issuing a SELL would open a NAKED SHORT — unlimited-loss liability on
calls. Refuse to act on positions we can't verify.

Module isolated from main.py's import chain so unit tests can import
it directly without ib_insync available.
"""

from __future__ import annotations


def option_qty_at_broker(
    broker, sym: str, strike: float, expiry: str, right: str = "C"
) -> float | None:
    """Return IB quantity for the exact option, or None on query failure."""
    try:
        for p in broker.ib.positions():
            c = p.contract
            if (
                getattr(c, "secType", "") == "OPT"
                and c.symbol == sym
                and getattr(c, "lastTradeDateOrContractMonth", "") == expiry
                and abs(float(c.strike) - float(strike)) < 0.01
                and getattr(c, "right", "") == right
            ):
                return float(p.position)
    except Exception:
        return None
    return 0.0


def option_at_broker(broker, sym: str, strike: float, expiry: str, right: str = "C") -> bool:
    """Return True iff IB account holds an option matching all of:
       symbol, strike (within $0.01), expiry (YYYYMMDD), right (C/P),
       qty > 0.

    Conservatively returns False on broker exception — better to skip
    a real position once than risk a phantom SELL.
    """
    qty = option_qty_at_broker(broker, sym, strike, expiry, right)
    return bool(qty is not None and qty > 0)
