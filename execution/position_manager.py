"""Position lifecycle — pyramid layers, trailing stops, profit rules.

State is persisted to state/positions.json, keyed by symbol.

Per-position record:
{
  "symbol": "AAPL",
  "layer": 0,               # 0=pilot, 1=half, 2=full
  "entry": 150.0,           # first buy price (pilot entry)
  "shares": 25,
  "stop": 138.0,
  "initial_stop": 138.0,
  "breakeven_moved": false, # 5% rule triggered
  "peak": 152.0,            # highest close since entry
  "layer_entries": [150.0]  # entry price of each layer
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from config import (
    DEFAULT_STOP_PCT,
    MAX_STOP_PCT,
    MIN_STOP_PCT,
    PILOT_FRACTIONS,
)

POSITIONS_FILE = Path("state/positions.json")
# Tuned 2026-04 via scripts/tune.py (combo_C)
PYRAMID_TRIGGER_PCT = 0.03  # +3% to add next layer (slower than 2% — cuts DD)
BREAKEVEN_R_MULTIPLE = 1.5  # BE at 1.5x initial risk. Tested 2.5R: helped
# bear defense and 2024-H2 but hurt 2023-H1, 2024-H1,
# 2025-H2 on net. Reverted. The real premature-exit
# bug is the trailing-MA rule (compresses stop to
# 50-DMA even when position is barely profitable) —
# addressed separately below.


@dataclass
class StopUpdate:
    symbol: str
    new_stop: float
    reason: str
    partial_exit_shares: int = 0


def load() -> dict:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save(positions: dict) -> None:
    from execution.safe_io import atomic_write_json

    atomic_write_json(POSITIONS_FILE, positions, indent=2, default=str)


def compute_initial_stop(entry: float, base_low: float | None = None) -> float:
    """Stop = below base low if available, else DEFAULT_STOP_PCT. Clamped 2-10%."""
    if base_low is not None and base_low > 0 and base_low < entry:
        pct = (entry - base_low) / entry
        pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, pct))
    else:
        pct = DEFAULT_STOP_PCT
    return entry * (1.0 - pct)


def open_pilot(symbol: str, price: float, shares: int, stop: float, positions: dict) -> dict:
    positions[symbol] = {
        "symbol": symbol,
        "layer": 0,
        "entry": price,
        "shares": shares,
        "stop": stop,
        "initial_stop": stop,
        "breakeven_moved": False,
        "peak": price,
        "layer_entries": [price],
    }
    return positions[symbol]


def should_advance_layer(pos: dict, current_price: float) -> bool:
    layer = pos.get("layer", 0)
    if layer >= len(PILOT_FRACTIONS) - 1:
        return False
    last_layer_entry = pos["layer_entries"][-1]
    return current_price >= last_layer_entry * (1.0 + PYRAMID_TRIGGER_PCT)


def advance_layer(pos: dict, price: float, add_shares: int) -> dict:
    pos["layer"] += 1
    pos["shares"] += add_shares
    pos["layer_entries"].append(price)
    return pos


def update_stop(pos: dict, df: pd.DataFrame) -> StopUpdate | None:
    """Apply stop-management rules in priority order. Returns an update if stop moved."""
    sym = pos["symbol"]
    if len(df) < 50:
        return None
    last_close = float(df["Close"].iloc[-1])
    pos["peak"] = max(pos.get("peak", last_close), last_close)

    entry = pos["entry"]
    stop = pos["stop"]
    new_stop = stop
    reason = ""
    partial = 0

    # Break-even stop move at 2×R (Minervini/O'Neil).
    # If stop was -6% below entry, move to BE at +12% gain. Scales with the
    # initial risk — no arbitrary percentage like +5%.
    initial_stop = float(pos.get("initial_stop") or pos.get("stop") or 0.0)
    gain_pct = (last_close - entry) / entry if entry > 0 else 0.0
    risk_pct = (entry - initial_stop) / entry if entry > 0 and initial_stop > 0 else 0.0
    be_trigger = risk_pct * BREAKEVEN_R_MULTIPLE
    if not pos.get("breakeven_moved") and risk_pct > 0 and gain_pct >= be_trigger:
        new_stop = max(new_stop, entry)
        pos["breakeven_moved"] = True
        reason = f"breakeven_at_2R_+{be_trigger * 100:.1f}pct"

    # Trailing MA — 21-DMA at full layer, 50-DMA while still building.
    # (Tested: applying 21-DMA to Power Trend leaders in early layers caused
    # premature stopouts. Keep the looser climax threshold in exits.py instead.)
    #
    # v1.2 tested a half-risk cap on the trail to prevent stopping out near BE
    # on small winners (NVT/LYB/META exited at 0% after peaking 7-8% in 2021-H1).
    # Both the cap and a plain "no-trail until 2R" version regressed total return
    # 10-30 alpha points — the trail IS doing real work on losing/stalling
    # positions, and loosening it bleeds more in failures than it saves in
    # stalled winners. Reverted to the original trail. The 0% exits appear to
    # be a small-sample noise pattern (few trades), not a systematic bug.
    sma21 = float(df["Close"].rolling(21).mean().iloc[-1])
    sma50 = float(df["Close"].rolling(50).mean().iloc[-1])
    layer = pos.get("layer", 0)
    trail = sma21 if layer >= 2 else sma50
    if trail > new_stop and trail < last_close:
        new_stop = trail
        if not reason:
            reason = f"trail_sma{'21' if layer >= 2 else '50'}:{trail:.2f}"

    if new_stop > stop + 1e-6:
        pos["stop"] = new_stop
        return StopUpdate(sym, new_stop, reason, partial)
    return None


def check_exit(pos: dict, last_close: float) -> str | None:
    """Hard exit rules. Returns exit reason or None."""
    if last_close <= pos["stop"]:
        return f"stop_hit:{last_close:.2f}<={pos['stop']:.2f}"
    return None


def close(symbol: str, positions: dict) -> None:
    positions.pop(symbol, None)
