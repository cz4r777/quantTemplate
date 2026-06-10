"""Tests for safety/phantom_guard.py — naked-short safety guard.

Imports the REAL helper (not a re-implementation) so drift between
production code and test is impossible.
"""

from __future__ import annotations

from types import SimpleNamespace

from safety.phantom_guard import option_at_broker, option_qty_at_broker


def _mk_broker(ib_positions):
    """Build a fake broker with given (symbol, strike, expiry, right, qty) tuples."""
    fake_positions = []
    for sym, strike, expiry, right, qty in ib_positions:
        contract = SimpleNamespace(
            secType="OPT",
            symbol=sym,
            strike=strike,
            lastTradeDateOrContractMonth=expiry,
            right=right,
        )
        fake_positions.append(SimpleNamespace(contract=contract, position=qty))
    return SimpleNamespace(ib=SimpleNamespace(positions=lambda: fake_positions))


def test_no_holdings_returns_false():
    assert not option_at_broker(_mk_broker([]), "SPY", 807.13, "20260821")
    assert option_qty_at_broker(_mk_broker([]), "SPY", 807.13, "20260821") == 0.0


def test_phantom_position_returns_false():
    """State has SPY 807.13/20260821, but IB only holds SPY 750/20260620."""
    broker = _mk_broker([("SPY", 750.0, "20260620", "C", 5)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821")


def test_exact_match_returns_true():
    broker = _mk_broker([("SPY", 807.13, "20260821", "C", 47)])
    assert option_at_broker(broker, "SPY", 807.13, "20260821")
    assert option_qty_at_broker(broker, "SPY", 807.13, "20260821") == 47.0


def test_qty_zero_returns_false():
    broker = _mk_broker([("SPY", 807.13, "20260821", "C", 0)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821")
    assert option_qty_at_broker(broker, "SPY", 807.13, "20260821") == 0.0


def test_short_position_returns_false():
    """Long-only bot — a short option (qty<0) shouldn't match."""
    broker = _mk_broker([("SPY", 807.13, "20260821", "C", -5)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821")


def test_strike_mismatch_returns_false():
    broker = _mk_broker([("SPY", 800.0, "20260821", "C", 47)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821")


def test_expiry_mismatch_returns_false():
    broker = _mk_broker([("SPY", 807.13, "20261016", "C", 47)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821")


def test_wrong_symbol_returns_false():
    broker = _mk_broker([("QQQ", 807.13, "20260821", "C", 47)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821")


def test_put_does_not_match_call():
    broker = _mk_broker([("SPY", 807.13, "20260821", "P", 47)])
    assert not option_at_broker(broker, "SPY", 807.13, "20260821", right="C")


def test_broker_exception_returns_false():
    """Conservative on errors — refuse to act rather than risk phantom SELL."""

    class BrokenBroker:
        @property
        def ib(self):
            raise RuntimeError("connection lost")

    assert not option_at_broker(BrokenBroker(), "SPY", 807.13, "20260821")
    assert option_qty_at_broker(BrokenBroker(), "SPY", 807.13, "20260821") is None


def test_float_position_999_does_not_truncate():
    """0.999 from float drift was the [S2] bug — int() would truncate to 0
    and treat a real holding as phantom. Now uses float(p.position) > 0."""
    broker = _mk_broker([("SPY", 807.13, "20260821", "C", 0.999)])
    assert option_at_broker(broker, "SPY", 807.13, "20260821")
    assert option_qty_at_broker(broker, "SPY", 807.13, "20260821") == 0.999
