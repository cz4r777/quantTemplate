"""T-BOT-LIVE-CCY-GUARD-FIX1 — held-cash + override currency guard.

Replaces T-BOT-LIVE-ENABLEMENT1's base-currency equality predicate,
which incorrectly refused every USD contract on an AUD-base account
even when USD cash was actually held.

New predicate accepts a contract currency when:
  - it appears in the account's held cash currencies, OR
  - it appears in the ACCEPTED_CONTRACT_CURRENCIES env override

Otherwise refuses with a descriptive reason. Empty contract currency
always refuses. Empty held + empty override always refuses.
"""

from __future__ import annotations

import importlib
import sys

from allocation.position_sizer import currency_mismatch_reason


def test_usd_contract_with_usd_cash_allows():
    assert currency_mismatch_reason({"USD"}, set(), "USD") is None


def test_aud_base_usd_cash_usd_contract_allows():
    # Operator's actual shape: AUD base, USD cash held, USD instrument.
    assert currency_mismatch_reason({"AUD", "USD"}, set(), "USD") is None


def test_no_usd_cash_no_override_refuses():
    reason = currency_mismatch_reason({"AUD"}, set(), "USD")
    assert reason is not None
    assert "USD" in reason
    assert "ACCEPTED_CONTRACT_CURRENCIES" in reason


def test_override_allows_when_cash_absent():
    assert currency_mismatch_reason({"AUD"}, {"USD"}, "USD") is None


def test_lookup_failure_with_override_allows():
    assert currency_mismatch_reason(set(), {"USD"}, "USD") is None


def test_lookup_failure_no_override_refuses():
    reason = currency_mismatch_reason(set(), set(), "USD")
    assert reason is not None
    assert "ACCEPTED_CONTRACT_CURRENCIES" in reason
    assert "auto-FX" in reason


def test_lookup_failure_none_inputs_refuses():
    reason = currency_mismatch_reason(None, None, "USD")
    assert reason is not None


def test_contract_currency_empty_refuses():
    assert currency_mismatch_reason({"USD"}, {"USD"}, "") is not None
    assert currency_mismatch_reason({"USD"}, {"USD"}, None) is not None


def test_case_insensitive_inputs():
    # Lower-case env / lower-case currency tokens normalize to upper.
    assert currency_mismatch_reason({"usd"}, set(), "USD") is None
    assert currency_mismatch_reason(set(), {"usd"}, "usd") is None


def test_refusal_string_has_no_secrets():
    reason = currency_mismatch_reason({"AUD"}, set(), "USD")
    assert reason is not None
    # No account ids / tokens leak through.
    assert "YOUR_ACCOUNT_ID" not in reason
    assert "ACCOUNT" not in reason.upper().replace("ACCOUNT CASH", "")


# ── ACCEPTED_CONTRACT_CURRENCIES env parsing ────────────────────────


def _reload_config():
    sys.modules.pop("config", None)
    return importlib.import_module("config")


def test_env_single_currency(monkeypatch):
    monkeypatch.setenv("ACCEPTED_CONTRACT_CURRENCIES", "USD")
    cfg = _reload_config()
    assert cfg.accepted_contract_currencies() == {"USD"}


def test_env_multiple_currencies_normalize(monkeypatch):
    monkeypatch.setenv("ACCEPTED_CONTRACT_CURRENCIES", "usd, aud ,  cad")
    cfg = _reload_config()
    assert cfg.accepted_contract_currencies() == {"USD", "AUD", "CAD"}


def test_env_unset_returns_empty_set(monkeypatch):
    monkeypatch.delenv("ACCEPTED_CONTRACT_CURRENCIES", raising=False)
    cfg = _reload_config()
    assert cfg.accepted_contract_currencies() == set()


def test_env_whitespace_only_returns_empty(monkeypatch):
    monkeypatch.setenv("ACCEPTED_CONTRACT_CURRENCIES", "   ,  ,  ")
    cfg = _reload_config()
    assert cfg.accepted_contract_currencies() == set()


def test_env_empty_string_returns_empty(monkeypatch):
    monkeypatch.setenv("ACCEPTED_CONTRACT_CURRENCIES", "")
    cfg = _reload_config()
    assert cfg.accepted_contract_currencies() == set()
