"""Tests for option-entry selection AND for the T-P0-STRIKEGRID1 refusal
paths that block invalid contracts from being tracked as open positions."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

# main.py imports broker/ibkr_client.py which imports ib_insync. Stub it so
# this test can collect on the Windows host (matches the pattern in
# test_no_stock_rebalance.py).
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

from brain.options import option_selector
from brain.options.option_selector import (
    _round_call_strike,
    _snap_to_listed_strike,
    select_contract,
    set_chain_provider,
)

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def _load_bot_main():
    spec = importlib.util.spec_from_file_location("options_v12_main_option_selector_tests", MAIN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bot_main = _load_bot_main()


def _df(close: float, rows: int = 40) -> pd.DataFrame:
    prices = [close * (1 + 0.002 * ((i % 5) - 2)) for i in range(rows - 1)]
    prices.append(close)
    return pd.DataFrame(
        {
            "Close": prices,
            "Open": prices,
            "High": [p * 1.01 for p in prices],
            "Low": [p * 0.99 for p in prices],
            "Volume": [1_000_000] * rows,
        }
    )


def test_round_call_strike_uses_standard_grid():
    assert _round_call_strike(807.13, stock_price=720.65) == 810.0
    assert _round_call_strike(755.05, stock_price=674.15) == 760.0
    assert _round_call_strike(112.12, stock_price=100.11) == 113.0
    assert _round_call_strike(24.26, stock_price=21.66) == 24.5


def test_select_contract_does_not_emit_penny_strike():
    contract = select_contract(_df(720.65), "SPY", "2026-05-14")

    assert contract is not None
    assert contract.strike == 810.0
    assert contract.expiry.endswith("18")


# ---------------------------------------------------------------------------
# T-OPT-STRIKEGRID2: snap to actual listed strikes from chain provider
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_default_provider():
    """Ensure no test leaks a default chain provider into another test.
    set_chain_provider is module-level state."""
    set_chain_provider(None)
    yield
    set_chain_provider(None)


def test_snap_picks_lowest_otm_listed_strike():
    # Raw 304.6 lands between 300 and 305; the lowest OTM listed strike
    # is 305 — exactly the TRGP-style case that motivated this ticket.
    out = _snap_to_listed_strike(
        304.6,
        "TRGP",
        "20260918",
        lambda sym, exp: [290, 295, 300, 305, 310, 315],
    )
    assert out == 305.0


def test_snap_skips_300_when_raw_target_is_305():
    # If IB only lists 5-point increments at this expiry (300, 310, 320),
    # raw 305 must NOT snap to 300 (ITM) — it must snap UP to 310.
    out = _snap_to_listed_strike(
        305.0,
        "TRGP",
        "20260918",
        lambda sym, exp: [290, 300, 310, 320, 330],
    )
    assert out == 310.0


def test_snap_returns_none_for_empty_chain():
    assert (
        _snap_to_listed_strike(
            305.0,
            "TRGP",
            "20260918",
            lambda s, e: [],
        )
        is None
    )


def test_snap_returns_none_when_provider_raises():
    def boom(_s, _e):
        raise RuntimeError("chain lookup failed")

    assert _snap_to_listed_strike(305.0, "TRGP", "20260918", boom) is None


def test_snap_returns_none_when_target_above_listed_top():
    # Raw target above every listed strike — strategy can't get OTM
    # with a real contract; refuse rather than silently going ITM.
    out = _snap_to_listed_strike(
        500.0,
        "TRGP",
        "20260918",
        lambda sym, exp: [300, 310, 320],
    )
    assert out is None


def test_snap_filters_zero_and_negative_strikes():
    # Defensive: IB has been known to return junk rows. Strip them.
    out = _snap_to_listed_strike(
        304.6,
        "TRGP",
        "20260918",
        lambda sym, exp: [-1.0, 0, 0.0, 300, 305, 310],
    )
    assert out == 305.0


def test_select_contract_uses_chain_provider_when_passed():
    # raw = 720.65 * 1.12 = 807.13. Listed: [800, 805, 815].
    # Lowest OTM >= 807.13 is 815.
    contract = select_contract(
        _df(720.65),
        "TRGP",
        "2026-05-14",
        chain_provider=lambda sym, exp: [790, 795, 800, 805, 815, 820],
    )
    assert contract is not None
    assert contract.strike == 815.0
    # The static-grid path would have produced 810.0 (see the existing
    # test_select_contract_does_not_emit_penny_strike). Confirms snap
    # beat the static grid here.


def test_select_contract_returns_none_when_chain_has_no_otm_strike():
    # Provider lists strikes ALL below raw target → no OTM contract
    # available → select_contract returns None (no fallback to static
    # grid, which is the whole point of the chain-truth path).
    contract = select_contract(
        _df(720.65),
        "TRGP",
        "2026-05-14",
        chain_provider=lambda sym, exp: [200, 250, 300, 400],
    )
    assert contract is None


def test_select_contract_returns_none_when_chain_provider_raises():
    def explode(_s, _e):
        raise RuntimeError("ib down")

    contract = select_contract(
        _df(720.65),
        "TRGP",
        "2026-05-14",
        chain_provider=explode,
    )
    assert contract is None


def test_default_chain_provider_engaged_when_no_arg():
    # The live wiring path: install once at startup, every select_contract
    # call uses it without the caller having to pass an arg.
    set_chain_provider(lambda sym, exp: [790, 800, 815, 820])
    contract = select_contract(_df(720.65), "TRGP", "2026-05-14")
    assert contract is not None
    assert contract.strike == 815.0


def test_default_provider_cleared_falls_back_to_static_grid():
    set_chain_provider(None)
    contract = select_contract(_df(720.65), "SPY", "2026-05-14")
    assert contract is not None
    # Static-grid behavior preserved when no provider is installed.
    assert contract.strike == 810.0


def test_explicit_chain_provider_overrides_default():
    # Default says [810, 820]. Per-call says [815, 825]. Per-call wins.
    set_chain_provider(lambda sym, exp: [810, 820])
    contract = select_contract(
        _df(720.65),
        "TRGP",
        "2026-05-14",
        chain_provider=lambda sym, exp: [815, 825],
    )
    assert contract is not None
    assert contract.strike == 815.0


def test_select_contract_passes_correct_expiry_to_chain_provider():
    # The provider is called with the SAME yyyymmdd expiry the
    # contract gets stamped with — otherwise the snapped strike could
    # be valid for one expiry and rejected at another.
    seen: dict = {}

    def capture(sym, exp):
        seen["sym"] = sym
        seen["exp"] = exp
        return [810, 820]

    contract = select_contract(
        _df(720.65),
        "TRGP",
        "2026-05-14",
        chain_provider=capture,
    )
    assert contract is not None
    assert seen["sym"] == "TRGP"
    assert seen["exp"] == contract.expiry


# ---------------------------------------------------------------------------
# T-OPT-STRIKEGRID2-WIRE1: live wiring of broker.list_option_strikes
# ---------------------------------------------------------------------------
# The selector's chain_provider mechanism is useless until main.py installs
# the broker's chain-query method. _install_option_chain_provider is the
# single wiring point — these tests pin its contract.


def test_install_option_chain_provider_wires_broker_fn():
    def sentinel(sym, exp):
        return [300.0, 310.0]

    broker = SimpleNamespace(list_option_strikes=sentinel)
    assert bot_main._install_option_chain_provider(broker) is True
    assert option_selector._default_chain_provider is sentinel


def test_install_option_chain_provider_skips_broker_without_method():
    # Default-cleared autouse fixture leaves _default_chain_provider as None.
    broker = SimpleNamespace()  # no list_option_strikes attribute
    assert bot_main._install_option_chain_provider(broker) is False
    assert option_selector._default_chain_provider is None


def test_install_option_chain_provider_skips_non_callable():
    broker = SimpleNamespace(list_option_strikes="not-callable")
    assert bot_main._install_option_chain_provider(broker) is False
    assert option_selector._default_chain_provider is None


def test_install_option_chain_provider_clears_stale_provider_on_skip():
    # A prior cycle may have installed a provider on a different broker.
    # If the new broker can't honor the contract, we must clear — not
    # leave a stale provider hanging.
    option_selector.set_chain_provider(lambda s, e: [100.0])
    assert option_selector._default_chain_provider is not None
    bot_main._install_option_chain_provider(SimpleNamespace())
    assert option_selector._default_chain_provider is None


def test_select_contract_after_wiring_uses_broker_chain():
    # End-to-end: install a fake broker, then call select_contract WITHOUT
    # an explicit chain_provider kwarg. The wired path must take effect.
    broker = SimpleNamespace(
        list_option_strikes=lambda sym, exp: [800.0, 815.0, 830.0],
    )
    bot_main._install_option_chain_provider(broker)
    contract = select_contract(_df(720.65), "TRGP", "2026-05-14")
    assert contract is not None
    # Raw target = 720.65 * 1.12 = 807.13. Lowest listed strike >= 807.13
    # is 815 — static grid (810) would have rejected.
    assert contract.strike == 815.0


def test_select_contract_after_failed_wiring_falls_back_to_static_grid():
    # broker with no chain method -> wiring returns False, provider stays
    # cleared, static grid path runs as before. No regression.
    bot_main._install_option_chain_provider(SimpleNamespace())
    contract = select_contract(_df(720.65), "SPY", "2026-05-14")
    assert contract is not None
    assert contract.strike == 810.0


# ---------------------------------------------------------------------------
# T-P0-STRIKEGRID1: refusal-path tests for _open_long_call_entry
# ---------------------------------------------------------------------------
# Two specific outcomes must NOT produce a tracked-state write:
#   1. broker returns status="invalid_contract"
#      (e.g. IB had no security definition for that strike + expiry)
#   2. broker returns status="submitted" but post-submit re-query at IB
#      shows 0 contracts held for the exact (sym, expiry, strike, right)
# Both routes used to lead to phantom rows in positions.json which the
# reconcile loop then cleaned up on a later cycle — and the same gate then
# refired entry, producing the open->phantom->clean->reopen loop.


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


def _seed_open_long_call_inputs(monkeypatch, broker_rec, broker_held_after):
    """Patch the bits _open_long_call_entry depends on so the test exercises
    only the rec.status / post-submit re-query branches."""
    contract = SimpleNamespace(
        symbol="TRGP",
        strike=305.0,
        expiry="20260918",
        premium=5.0,
        dte_days=120,
        entry_date="",
    )
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
    broker = MagicMock()
    broker.place_option_order.return_value = broker_rec
    broker._option_position_qty.return_value = broker_held_after
    # T-BOT-LIVE-CCY-GUARD-FIX1 — currency guard reads
    # broker.account_cash_currencies(). Tests trade USD instruments; stub
    # a held USD cash row so the guard is satisfied and the test
    # continues to exercise its real path.
    broker.account_cash_currencies.return_value = {"USD"}
    return broker, contract


def test_invalid_contract_refuses_to_write_tracked_state(monkeypatch):
    rec = {
        "status": "invalid_contract",
        "ib_status": "NoSecurityDefinition",
        "error": "no security definition for TRGP $305C 20260918",
    }
    broker, _ = _seed_open_long_call_inputs(monkeypatch, rec, broker_held_after=0.0)
    positions: dict = {}

    result = bot_main._open_long_call_entry(
        sym="TRGP",
        df=_entry_df(),
        equity=100_000,
        broker=broker,
        positions=positions,
        risk_multiplier=1.0,
        entry_signal="stock_breakout",
    )

    assert result is None
    assert positions == {}
    broker.place_option_order.assert_called_once()
    # Re-query is not even reached when status=invalid_contract.
    broker._option_position_qty.assert_not_called()


def test_post_submit_zero_held_refuses_to_write_tracked_state(monkeypatch):
    # Broker says order was submitted (no terminal cancel inside its
    # wait window), but the live IB position for this contract is still
    # 0. Common pattern: IB delivers Error 200 async after the broker's
    # wait_secs deadline.
    rec = {"status": "submitted", "ib_status": "Submitted", "order_id": 12345}
    broker, _ = _seed_open_long_call_inputs(monkeypatch, rec, broker_held_after=0.0)
    positions: dict = {}

    result = bot_main._open_long_call_entry(
        sym="TRGP",
        df=_entry_df(),
        equity=100_000,
        broker=broker,
        positions=positions,
        risk_multiplier=1.0,
        entry_signal="stock_breakout",
    )

    assert result is None
    assert positions == {}
    broker.place_option_order.assert_called_once()
    broker._option_position_qty.assert_called_once_with("TRGP", "20260918", 305.0, "C")


def test_post_submit_confirmed_held_writes_tracked_state(monkeypatch):
    # Healthy path: broker submitted AND IB confirms 1 held. State gets
    # written; this is the regression check that the new guards don't
    # break the normal entry flow.
    rec = {"status": "submitted", "ib_status": "Filled", "order_id": 12346}
    broker, _ = _seed_open_long_call_inputs(monkeypatch, rec, broker_held_after=1.0)
    positions: dict = {}

    result = bot_main._open_long_call_entry(
        sym="TRGP",
        df=_entry_df(),
        equity=100_000,
        broker=broker,
        positions=positions,
        risk_multiplier=1.0,
        entry_signal="stock_breakout",
    )

    assert result is not None
    assert result["action"] == "open_option"
    assert positions.get("TRGP", {}).get("contracts") == 1
