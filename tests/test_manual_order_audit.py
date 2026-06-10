"""Regression tests for manual-order scripts: audit-log wiring and cover-short
sign correctness.

These tests are static (read source, regex-match) so they run on any host,
including the Windows dev venv that lacks ib_insync. The point is to lock in
two invariants that the 2026-05-08 incident broke:

  1. Every script that connects to IB and issues orders must call
     exec_log.wrap_broker() before placing them, so the broker_* row appears
     in state/exec_log.jsonl and the audit chain is complete.

  2. cover_short.py must convert short positions (negative qty) into BUY orders
     with positive contracts. Earlier code emitted action='SELL' contracts=-N
     which IB rejected with Error 321 every time and never closed the short.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_sell_option_wraps_broker_for_audit():
    src = _read("sell_option.py")
    assert "exec_log.wrap_broker(broker)" in src, (
        "sell_option.py must call wrap_broker(broker) so place_option_order "
        "calls auto-emit broker_option_sell rows in state/exec_log.jsonl"
    )


def test_buy_option_wraps_broker_for_audit():
    assert "exec_log.wrap_broker(broker)" in _read("buy_option.py")


def test_sell_position_wraps_broker_for_audit():
    assert "exec_log.wrap_broker(broker)" in _read("sell_position.py")


def test_buy_position_wraps_broker_for_audit():
    assert "exec_log.wrap_broker(broker)" in _read("buy_position.py")


def test_cover_short_wraps_broker_for_audit():
    assert "exec_log.wrap_broker(broker)" in _read("cover_short.py")


def test_cover_short_option_uses_buy_with_positive_contracts():
    src = _read("cover_short.py")
    # qty is negative for shorts; buy_qty = -qty inverts the sign to positive.
    assert re.search(r"buy_qty\s*=\s*-qty", src), (
        "cover_short.py option path must compute buy_qty = -qty so the broker "
        "receives a positive contract count (IB Err 321 on negatives)"
    )
    # The place_option_order call must use action="BUY" and pass buy_qty.
    assert re.search(r'action\s*=\s*"BUY"', src), 'cover_short.py option path must use action="BUY"'
    assert re.search(r"contracts\s*=\s*buy_qty", src), (
        "cover_short.py option path must pass contracts=buy_qty (positive)"
    )


def test_cover_short_option_uses_mkt_not_midprice():
    src = _read("cover_short.py")
    # MIDPRICE is rejected on options/SMART with Err 387. Cover-short is a
    # safety op — pay the spread for a guaranteed fill. Match an actual
    # order_type='MIDPRICE' assignment, not a stray comment mention.
    assert not re.search(r"""order_type\s*=\s*['"]MIDPRICE['"]""", src), (
        "cover_short.py must not use order_type='MIDPRICE' on options/SMART "
        "(IB Err 387). Use 'MKT' for guaranteed fill on cover orders."
    )
    assert re.search(r'order_type\s*=\s*"MKT"', src), (
        'cover_short.py option path must use order_type="MKT"'
    )
