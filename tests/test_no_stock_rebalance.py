"""Regression: options-v1.2 main.py must not call broker.rebalance during
the normal cycle. 2026-05-08 P0: stock-side legacy paths in manage_existing
and consider_new_entries swept six unrelated IB stock longs.

These tests assert three invariants:
  1. No active broker.rebalance(...) call site in main.py source.
  2. manage_existing skips stock-shape positions without touching the
     broker.
  3. consider_new_entries opens stock-underlying breakouts as long calls,
     never as stock orders.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

if "ib_insync" not in sys.modules:
    fake_ib = types.ModuleType("ib_insync")

    class _FakeIBObj:
        _POSITIONAL = ("action", "totalQuantity", "lmtPrice")

        def __init__(self, *args, **kwargs):
            for i, val in enumerate(args):
                if i < len(self._POSITIONAL):
                    setattr(self, self._POSITIONAL[i], val)
            for k, v in kwargs.items():
                setattr(self, k, v)

    fake_ib.IB = _FakeIBObj
    fake_ib.Stock = _FakeIBObj
    fake_ib.Option = _FakeIBObj
    fake_ib.MarketOrder = _FakeIBObj
    fake_ib.LimitOrder = _FakeIBObj
    fake_ib.Order = _FakeIBObj
    sys.modules["ib_insync"] = fake_ib

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def _load_bot_main():
    spec = importlib.util.spec_from_file_location("options_v12_main_no_stock_tests", MAIN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bot_main = _load_bot_main()


def test_main_has_no_active_broker_rebalance():
    src = MAIN_PY.read_text()
    pattern = re.compile(r"^\s*(?:[a-z_][a-z_0-9]*\s*=\s*)?broker\.rebalance\s*\(", re.MULTILINE)
    matches = pattern.findall(src)
    assert matches == [], (
        f"main.py has {len(matches)} active broker.rebalance call(s); "
        "options-v1.2 normal cycle must not touch stocks."
    )


def _stock_pos() -> dict:
    return {
        "symbol": "AAPL",
        "shares": 10,
        "entry": 180.0,
        "stop": 170.0,
        "peak": 185.0,
        "layer": 0,
        "initial_stop": 170.0,
    }


def _option_pos() -> dict:
    return {
        "symbol": "SPY",
        "strike": 580.0,
        "expiry": "20260620",
        "right": "C",
        "contracts": 5,
        "premium_entry": 4.20,
        "iv_entry": 0.18,
        "contracts_entered": 5,
        "peak_option_value": 4.50,
        "partial_2x_taken": False,
        "entry_date": "2026-05-05",
        "dte_days": 45,
        "entry_stock_price": 580.0,
    }


def _df():
    return pd.DataFrame(
        {
            "Close": [100.0] * 60,
            "Low": [98.0] * 60,
            "High": [102.0] * 60,
            "Volume": [1_000_000] * 60,
        }
    )


def test_manage_existing_skips_stock_shape_without_broker_call():
    broker = MagicMock()
    positions = {"AAPL": _stock_pos()}
    dfs = {"AAPL": _df()}

    actions = bot_main.manage_existing(positions, dfs, broker)

    broker.rebalance.assert_not_called()
    broker.place_option_order.assert_not_called()
    assert positions == {"AAPL": _stock_pos()}, "stock entry must remain untouched"
    assert any(a["action"] == "skip" and "stock" in a["reason"] for a in actions), (
        f"expected stock-skip action, got {actions}"
    )


def test_manage_existing_still_routes_options(monkeypatch):
    """Option-shape position must still flow into the option manager path.
    We assert manage_existing does NOT call broker.rebalance even on the
    option path; option exits use broker.place_option_order via
    _manage_option_position."""
    broker = MagicMock()
    # Phantom-guard returns 0 for an unknown contract, which makes
    # _manage_option_position skip cleanly without any IB call.
    monkeypatch.setattr(bot_main, "_option_qty_at_broker", lambda *a, **k: 0.0)
    positions = {"SPY": _option_pos()}
    dfs = {"SPY": _df()}

    bot_main.manage_existing(positions, dfs, broker)

    broker.rebalance.assert_not_called()


def test_manage_existing_removes_confirmed_phantom_after_threshold(monkeypatch):
    broker = MagicMock()
    monkeypatch.setattr(bot_main, "_option_qty_at_broker", lambda *a, **k: 0.0)
    monkeypatch.setattr(bot_main, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(bot_main, "send", lambda *a, **k: None)
    pos = _option_pos()
    pos["phantom_not_held_count"] = bot_main.PHANTOM_DELETE_AFTER - 1
    positions = {"SPY": pos}

    actions = bot_main.manage_existing(positions, {"SPY": _df()}, broker)

    broker.rebalance.assert_not_called()
    broker.place_option_order.assert_not_called()
    assert positions == {}
    assert actions[0]["action"] == "remove_phantom_option"


def test_exercise_guard_closes_untracked_expiring_option(monkeypatch):
    monkeypatch.setattr(bot_main, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(bot_main, "send", lambda *a, **k: None)
    expiry = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d")
    contract = SimpleNamespace(
        secType="OPT",
        symbol="AGX",
        strike=710.0,
        lastTradeDateOrContractMonth=expiry,
        right="C",
    )
    broker = MagicMock()
    broker.ib.positions.return_value = [SimpleNamespace(contract=contract, position=1)]
    broker.ib.openTrades.return_value = []
    broker.place_option_order.return_value = {
        "status": "submitted",
        "ib_status": "Submitted",
    }

    actions = bot_main._close_untracked_expiring_options(broker, positions={})

    broker.place_option_order.assert_called_once_with(
        symbol="AGX",
        expiry=expiry,
        strike=710.0,
        right="C",
        action="SELL",
        contracts=1,
        order_type="MKT",
        wait_secs=10.0,
    )
    assert actions[0]["action"] == "exercise_guard_close"


def test_exercise_guard_ignores_tracked_expiring_option(monkeypatch):
    monkeypatch.setattr(bot_main, "log_event", lambda *a, **k: None)
    expiry = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d")
    contract = SimpleNamespace(
        secType="OPT",
        symbol="AGX",
        strike=710.0,
        lastTradeDateOrContractMonth=expiry,
        right="C",
    )
    broker = MagicMock()
    broker.ib.positions.return_value = [SimpleNamespace(contract=contract, position=1)]

    actions = bot_main._close_untracked_expiring_options(
        broker, positions={"AGX": {**_option_pos(), "strike": 710.0, "expiry": expiry}}
    )

    broker.place_option_order.assert_not_called()
    assert actions == []


def _entry_df():
    rows = 220
    return pd.DataFrame(
        {
            "Open": [100.0] * rows,
            "Close": [100.0] * rows,
            "Low": [98.0] * rows,
            "High": [102.0] * rows,
            "Volume": [1_000_000] * rows,
        }
    )


def test_non_index_breakout_opens_long_call_not_stock_rebalance(monkeypatch):
    broker = MagicMock()
    broker.place_option_order.return_value = {"status": "submitted"}
    # T-P0-STRIKEGRID1 added a post-submit re-query in _open_long_call_entry.
    # Stub a positive held-qty so the test exercises the healthy entry path
    # without tripping the new "unconfirmed at broker" refusal.
    broker._option_position_qty.return_value = 1.0
    # T-BOT-LIVE-CCY-GUARD-FIX1 — currency guard reads
    # broker.account_cash_currencies(). Tests trade USD instruments; stub
    # a held USD cash row so the guard is satisfied and the test
    # continues to exercise its real path.
    broker.account_cash_currencies.return_value = {"USD"}
    positions = {}
    contract = SimpleNamespace(
        symbol="NVDA",
        strike=120.0,
        expiry="20260918",
        premium=5.0,
        dte_days=120,
        entry_date="",
    )
    breakout = SimpleNamespace(is_breakout=True, is_pocket_pivot=False, base_count=1)
    tt = SimpleNamespace(passes=True, gates={})

    monkeypatch.setattr(bot_main, "trend_template", lambda *a, **k: tt)
    monkeypatch.setattr(bot_main, "detect_breakout", lambda *a, **k: breakout)
    monkeypatch.setattr(bot_main, "earnings_blackout", lambda *a, **k: (False, ""))
    monkeypatch.setattr(bot_main, "select_contract", lambda *a, **k: contract)
    monkeypatch.setattr(bot_main, "option_contracts", lambda *a, **k: 1)
    monkeypatch.setattr(bot_main, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(bot_main, "send", lambda *a, **k: None)
    monkeypatch.setattr(
        bot_main,
        "open_option_position",
        lambda c, n, p: p.__setitem__(
            c.symbol,
            {
                "contracts": n,
                "premium_entry": c.premium,
                "strike": c.strike,
                "expiry": c.expiry,
            },
        ),
    )

    actions = bot_main.consider_new_entries(
        candidates=["NVDA"],
        dfs={"NVDA": _entry_df()},
        rs_ranks={"NVDA": 99},
        positions=positions,
        equity=100_000,
        broker=broker,
    )

    broker.rebalance.assert_not_called()
    broker.place_option_order.assert_called_once_with(
        symbol="NVDA",
        expiry="20260918",
        strike=120.0,
        right="C",
        action="BUY",
        contracts=1,
        order_type="MID",
    )
    assert actions[0]["action"] == "open_option"
    assert actions[0]["entry_signal"] == "stock_breakout"
    assert positions["NVDA"]["contracts"] == 1
