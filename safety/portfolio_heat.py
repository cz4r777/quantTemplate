"""Portfolio heat — aggregate risk across all open positions.

    heat = Σ (shares × |entry − stop|) / total_equity

Target ≤ 3%. Hard ceiling 5%. When heat ≥ ceiling, no new entries until
existing positions move stops up and heat drops.
"""

from __future__ import annotations

HEAT_TARGET = 0.03
HEAT_CEILING = 0.05


def compute_heat(positions: dict, equity: float) -> float:
    """
    positions: {sym: {"shares": int, "entry": float, "stop": float, ...}}
    """
    if equity <= 0:
        return 0.0
    total_risk = 0.0
    for p in positions.values():
        shares = p.get("shares") or 0
        entry = p.get("entry") or 0
        stop = p.get("stop") or 0
        if shares <= 0 or entry <= 0 or stop <= 0:
            continue
        total_risk += shares * max(entry - stop, 0)
    return total_risk / equity


def can_add_risk(positions: dict, equity: float, new_trade_risk: float) -> tuple[bool, str]:
    current = compute_heat(positions, equity)
    projected = current + (new_trade_risk / equity if equity > 0 else 0.0)
    if projected > HEAT_CEILING:
        return False, f"heat_ceiling:{projected:.2%}>{HEAT_CEILING:.0%}"
    return True, f"ok:{projected:.2%}"
