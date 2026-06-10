"""T-BOT-LIVE-CCY-GUARD-FIX1 — broker.account_cash_currencies() coverage.

Tests use a fake ib.accountValues() that yields tag/currency/value rows,
mirroring the shape ib_insync returns. No real IB connection involved.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

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

from broker.ibkr_client import IBKRClient


def _row(tag: str, currency: str, value):
    return SimpleNamespace(tag=tag, currency=currency, value=value)


def _client_with_rows(rows):
    c = IBKRClient()

    class _IB:
        def accountValues(self):
            return list(rows)

    c.ib = _IB()
    return c


def test_returns_usd_when_usd_cash_present():
    c = _client_with_rows(
        [
            _row("NetLiquidation", "AUD", "42915.53"),  # base label only
            _row("CashBalance", "USD", "5000.00"),
            _row("CashBalance", "AUD", "1000.00"),
        ]
    )
    assert c.account_cash_currencies() == {"USD", "AUD"}


def test_skips_zero_or_negative_cash():
    c = _client_with_rows(
        [
            _row("CashBalance", "USD", "0.00"),
            _row("CashBalance", "EUR", "-100.00"),
            _row("CashBalance", "AUD", "1000.00"),
        ]
    )
    assert c.account_cash_currencies() == {"AUD"}


def test_aggregates_multiple_cash_tags():
    c = _client_with_rows(
        [
            _row("TotalCashBalance", "USD", "1.00"),
            _row("AvailableFunds", "GBP", "1.00"),
            _row("SettledCash", "JPY", "1.00"),
            _row("TotalCashValue", "CAD", "1.00"),
        ]
    )
    assert c.account_cash_currencies() == {"USD", "GBP", "JPY", "CAD"}


def test_ignores_base_pseudo_currency():
    c = _client_with_rows(
        [
            _row("CashBalance", "BASE", "1000.00"),
            _row("CashBalance", "USD", "1.00"),
        ]
    )
    assert c.account_cash_currencies() == {"USD"}


def test_ignores_non_cash_tags():
    c = _client_with_rows(
        [
            _row("NetLiquidation", "AUD", "42915.53"),
            _row("BuyingPower", "AUD", "137726.89"),
            _row("InitMarginReq", "AUD", "22256.50"),
            _row("MaintMarginReq", "AUD", "21309.04"),
            # No cash-tagged rows -> empty result
        ]
    )
    assert c.account_cash_currencies() == set()


def test_lookup_failure_returns_empty():
    c = IBKRClient()

    class _IB:
        def accountValues(self):
            raise RuntimeError("connection lost")

    c.ib = _IB()
    assert c.account_cash_currencies() == set()


def test_unparseable_value_is_skipped():
    c = _client_with_rows(
        [
            _row("CashBalance", "USD", "not-a-number"),
            _row("CashBalance", "AUD", "1.00"),
        ]
    )
    assert c.account_cash_currencies() == {"AUD"}


def test_operator_account_shape_aud_base_usd_cash():
    # Mirrors the live YOUR_ACCOUNT_ID shape per Gateway Diagnostics 2026-05-18.
    rows = [
        _row("NetLiquidation", "AUD", "42915.53"),
        _row("AvailableFunds", "AUD", "20659.03"),
        _row("BuyingPower", "AUD", "137726.89"),
        _row("InitMarginReq", "AUD", "22256.50"),
        _row("MaintMarginReq", "AUD", "21309.04"),
        # operator-provided USD cash:
        _row("CashBalance", "USD", "1500.00"),
    ]
    c = _client_with_rows(rows)
    held = c.account_cash_currencies()
    assert "USD" in held
    # Guard predicate would therefore ALLOW a USD contract.
    from allocation.position_sizer import currency_mismatch_reason

    assert currency_mismatch_reason(held, set(), "USD") is None
