"""T-BOT-LIVE-ENABLEMENT1 — no-manage flag honored in manage_existing.

Positions seeded by seed_existing_positions.py carry manage=False.
manage_existing must skip those rows entirely — no exit, no trim, no
pyramid, no exercise. Positions that don't carry the flag at all (or
carry manage=True) continue to flow through the existing management
path.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

# Stub ib_insync so this test can collect on hosts without IB installed.
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
    spec = importlib.util.spec_from_file_location("options_v12_main_no_manage_tests", MAIN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bot_main = _load_bot_main()


def _df() -> pd.DataFrame:
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


@pytest.fixture
def silent_log(monkeypatch):
    seen: list[tuple] = []

    def fake_log_event(name, **kw):
        seen.append((name, kw))

    monkeypatch.setattr(bot_main, "log_event", fake_log_event)
    return seen


def test_manage_existing_skips_manage_false_rows(monkeypatch, silent_log):
    # Position with manage=False must NEVER reach the option-management
    # path. Patch _manage_option_position so we can assert it was not
    # called for this symbol.
    called_with: list[str] = []

    def fake_manage_option_position(sym, pos, df, broker):
        called_with.append(sym)
        return None

    monkeypatch.setattr(bot_main, "_manage_option_position", fake_manage_option_position)

    # Don't actually trigger circuit breaker
    class _CB:
        def kill_switch_active(self):
            return False

    monkeypatch.setattr(bot_main, "CircuitBreaker", _CB)

    positions = {
        "AAPL": {
            "contracts": 20,
            "premium_entry": 1.19,
            "strike": 340.0,
            "expiry": "20260717",
            "right": "C",
            "manage": False,
            "claimed": True,
        },
        "MSFT": {
            "contracts": 5,
            "premium_entry": 2.50,
            "strike": 400.0,
            "expiry": "20260717",
            "right": "C",
            # no manage flag -> default True
        },
    }
    dfs = {"AAPL": _df(), "MSFT": _df()}

    bot_main.manage_existing(positions, dfs, broker=None)

    # AAPL must be skipped via the no-manage path.
    assert "AAPL" not in called_with
    # MSFT (default-manage) must still flow through.
    assert "MSFT" in called_with
    # Skip event must be logged so operators see the reason.
    skip_events = [e for e in silent_log if e[0] == "skip_unmanaged"]
    assert any(kw.get("symbol") == "AAPL" for _, kw in skip_events)


def test_explicit_manage_true_still_manages(monkeypatch, silent_log):
    called_with: list[str] = []
    monkeypatch.setattr(
        bot_main,
        "_manage_option_position",
        lambda sym, pos, df, broker: called_with.append(sym) or None,
    )

    class _CB:
        def kill_switch_active(self):
            return False

    monkeypatch.setattr(bot_main, "CircuitBreaker", _CB)
    positions = {
        "NVDA": {
            "contracts": 8,
            "premium_entry": 6.90,
            "strike": 260.0,
            "expiry": "20260717",
            "right": "C",
            "manage": True,
        },
    }
    dfs = {"NVDA": _df()}
    bot_main.manage_existing(positions, dfs, broker=None)
    assert "NVDA" in called_with
