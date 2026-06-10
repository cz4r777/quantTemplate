"""Option position lifecycle — entry, re-pricing, hybrid exits.

Exit logic (first match wins):
  1. Stock-level stop    — underlying drops below entry × (1 - STOP_PCT) → close
  2. Stock-level trend   — underlying closes below 50-DMA → close
  3. Fast profit take    — option value ≥ 2× premium → sell 50%
  4. Big win             — option value ≥ 3× premium → sell remaining 50%
  5. Time decay          — DTE ≤ ROLL_DTE → roll or close
  6. Max loss            — option value ≤ 0.5× premium → close

Option positions are simpler than stock pyramid layers — no staged entries.
Every new signal opens a single contract batch at full sizing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from brain.options.option_selector import OptionContract, _historical_vol, reprice_contract

POSITIONS_FILE = Path("state/options_positions.json")

# Option-level exit thresholds (% of original premium)
PROFIT_TAKE_2X = 2.0
PROFIT_TAKE_3X = 3.0
MAX_LOSS_PCT = 0.5

# Stock-level exit thresholds
STOCK_STOP_PCT = 0.08  # underlying drops 8% below entry → close option
ROLL_DTE = 30  # roll/close when DTE ≤ 30


@dataclass
class OptionExitSignal:
    symbol: str
    action: str  # "close" | "trim" | "roll"
    reason: str
    close_contracts: int


def load() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save(positions: dict) -> None:
    from execution.safe_io import atomic_write_json

    atomic_write_json(POSITIONS_FILE, positions, indent=2, default=str)


def open_position(contract: OptionContract, contracts: int, positions: dict) -> dict:
    positions[contract.symbol] = {
        "symbol": contract.symbol,
        "entry_date": contract.entry_date,
        "entry_stock_price": contract.stock_price,
        "strike": contract.strike,
        "dte_days": contract.dte_days,
        "expiry": getattr(contract, "expiry", ""),
        "premium_entry": contract.premium,
        "iv_entry": contract.iv,
        "contracts": contracts,
        "contracts_entered": contracts,  # immutable snapshot
        "peak_option_value": contract.premium,
        "partial_2x_taken": False,
    }
    return positions[contract.symbol]


def close(symbol: str, positions: dict) -> None:
    positions.pop(symbol, None)


def days_elapsed(pos: dict, current_date: str) -> int:
    try:
        entry = pd.Timestamp(pos["entry_date"])
        now = pd.Timestamp(current_date)
        return max((now - entry).days, 0)
    except Exception:
        return 0


def current_option_value(pos: dict, df: pd.DataFrame, current_date: str) -> float:
    """Re-price the open contract using current stock price + decayed DTE."""
    stock_price = float(df["Close"].iloc[-1])
    elapsed = days_elapsed(pos, current_date)
    contract = OptionContract(
        symbol=pos["symbol"],
        stock_price=pos["entry_stock_price"],
        strike=pos["strike"],
        dte_days=pos["dte_days"],
        premium=pos["premium_entry"],
        iv=pos["iv_entry"],
        entry_date=pos["entry_date"],
    )
    current_hv = _historical_vol(df)
    return reprice_contract(contract, stock_price, elapsed, current_hv)


def evaluate_exit(pos: dict, df: pd.DataFrame, current_date: str) -> OptionExitSignal | None:
    """Check all exit conditions; return the first matching signal."""
    if not pos.get("contracts") or pos["contracts"] <= 0:
        return None

    sym = pos["symbol"]
    current_value = current_option_value(pos, df, current_date)
    pos["peak_option_value"] = max(pos.get("peak_option_value", 0), current_value)
    # Stash current re-priced premium for dashboard rendering. Updated each
    # cycle so the dashboard shows live option value vs entry premium.
    pos["current_premium"] = round(current_value, 2)
    pos["current_stock_price"] = round(float(df["Close"].iloc[-1]), 2)
    pos["last_update"] = current_date

    premium_entry = pos["premium_entry"]
    stock_price = float(df["Close"].iloc[-1])
    elapsed = days_elapsed(pos, current_date)

    # 1. Stock-level hard stop
    entry_stock = pos["entry_stock_price"]
    if stock_price <= entry_stock * (1 - STOCK_STOP_PCT):
        return OptionExitSignal(
            sym,
            "close",
            f"stock_stop:{(stock_price / entry_stock - 1) * 100:+.1f}pct",
            pos["contracts"],
        )

    # 2. Stock below 50-DMA (same trend-break rule as stock bot)
    if len(df) >= 50:
        sma50 = float(df["Close"].rolling(50).mean().iloc[-1])
        if stock_price < sma50 and elapsed > 5:
            return OptionExitSignal(
                sym, "close", f"stock_<50dma:{stock_price:.2f}<{sma50:.2f}", pos["contracts"]
            )

    # 3. Fast profit take — option up 2x, sell half
    if not pos.get("partial_2x_taken") and current_value >= premium_entry * PROFIT_TAKE_2X:
        half = pos["contracts"] // 2
        if half > 0:
            return OptionExitSignal(sym, "trim", f"profit_take_2x:{current_value:.2f}", half)

    # 4. Big win — option up 3x, sell remaining
    if current_value >= premium_entry * PROFIT_TAKE_3X:
        return OptionExitSignal(
            sym, "close", f"profit_take_3x:{current_value:.2f}", pos["contracts"]
        )

    # 5. Time decay — DTE ≤ ROLL_DTE → close (roll handled separately in v2.0)
    remaining_dte = pos["dte_days"] - elapsed
    if remaining_dte <= ROLL_DTE:
        return OptionExitSignal(sym, "close", f"expiry_near:{remaining_dte}dte", pos["contracts"])

    # 6. Max loss — preserve capital
    if current_value <= premium_entry * MAX_LOSS_PCT:
        return OptionExitSignal(sym, "close", f"max_loss:{current_value:.2f}", pos["contracts"])

    return None
