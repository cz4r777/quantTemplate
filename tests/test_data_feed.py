"""Sanity tests for brain/data_feed.py and watchlist source migration.

Catches regressions like the trading-day vs calendar-day units bug from
the start/end fallback branch (commit 5cb7cf1 → fixed). The principle
is simple: if the caller asks for N trading days, fetch_ohlcv must
return roughly N trading days back.

The T-WLIST-FFTY-SOURCE-MIGRATION1 tests live here too because the
ticket's allowed-files list nominates this file as the test home; the
watchlist builder has no dedicated test module yet.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from brain.data_feed import fetch_ohlcv


@pytest.mark.network
def test_fetch_ohlcv_short_lookback_uses_period_path():
    # < 1500 days takes the period= branch
    df = fetch_ohlcv("SPY", 100)
    assert len(df) >= 90, f"100-day request should return ~100 bars, got {len(df)}"


@pytest.mark.network
def test_fetch_ohlcv_long_lookback_returns_full_window():
    # > 1500 days takes the start/end branch — the regression target
    df = fetch_ohlcv("SPY", 1510)
    assert len(df) >= 1400, (
        f"5y request (1510 trading days) should return >=1400 bars, got {len(df)} — "
        f"likely a calendar-vs-trading-day units bug in start/end fallback"
    )


@pytest.mark.network
def test_fetch_ohlcv_very_long_lookback():
    # Stress the bug zone — 7+ years in trading days
    df = fetch_ohlcv("SPY", 1800)
    assert len(df) >= 1650, f"7y request should return >=1650 bars, got {len(df)}"


# ---------------------------------------------------------------------------
# T-WLIST-FFTY-SOURCE-MIGRATION1
# ---------------------------------------------------------------------------
# These tests pin the watchlist-source migration: FFTY is treated as
# RETIRED from the default combined-holdings feed (operator override via
# WATCHLIST_FFTY_URL stays available). The source_note must reflect
# retirement honestly; the failover safety guards must remain intact.


@pytest.fixture
def _watchlist_module(tmp_path, monkeypatch):
    """Import build_watchlist with state/ pointed at a tmp dir so the test
    can inspect the written watchlist.json without touching the live
    state/ directory. Cleans up sys.modules so each test gets a fresh
    import — the module runs OUT_DIR.mkdir at import time."""
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    # Stage a state/ dir tmp_path/state with empty sp500/ndx caches so
    # the fallback universe = MAG7 only (deterministic + small).
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "sp500_top50.json").write_text(json.dumps({"tickers": []}))
    (state_dir / "nasdaq100.json").write_text(json.dumps({"tickers": []}))
    # The module derives OUT_DIR from its own __file__. Point ROOT at
    # tmp_path via monkeypatching the module after import.
    sys.modules.pop("build_watchlist", None)
    mod = importlib.import_module("build_watchlist")
    monkeypatch.setattr(mod, "OUT_DIR", state_dir)
    # Skip the inline NDX rebuild during _ensure_nasdaq100 — tests don't
    # need network, and the empty cache we wrote is intentional.
    monkeypatch.setattr(mod, "_ensure_nasdaq100", lambda: len(mod._load_nasdaq100()))
    # Silence alert dispatch so tests don't ping smsbot.
    monkeypatch.setattr(mod, "alert_failure", lambda *a, **kw: None)
    yield mod, state_dir
    sys.modules.pop("build_watchlist", None)


def test_url_overridable_via_env(monkeypatch):
    monkeypatch.setenv("WATCHLIST_FFTY_URL", "https://example.invalid/successor.csv")
    sys.modules.pop("build_watchlist", None)
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    try:
        mod = importlib.import_module("build_watchlist")
        assert mod.HOLDINGS_URL == "https://example.invalid/successor.csv"
    finally:
        sys.modules.pop("build_watchlist", None)


def test_url_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("WATCHLIST_FFTY_URL", raising=False)
    sys.modules.pop("build_watchlist", None)
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    try:
        mod = importlib.import_module("build_watchlist")
        assert mod.HOLDINGS_URL == "https://www.innovatoretfs.com/etf/xt_holdings.csv"
    finally:
        sys.modules.pop("build_watchlist", None)


def test_retired_note_is_steady_state_label(_watchlist_module):
    mod, _ = _watchlist_module
    # Truthful label — no "empty" / "failover" wording that would imply
    # transience.
    assert "FFTY retired" in mod.FFTY_RETIRED_NOTE
    assert "empty" not in mod.FFTY_RETIRED_NOTE.lower()
    assert "failover" not in mod.FFTY_RETIRED_NOTE.lower()


def test_zero_ffty_extraction_writes_retired_source_note(_watchlist_module, monkeypatch):
    mod, state_dir = _watchlist_module

    # Fetch returns a CSV with no FFTY rows (mirrors the live 2026-05-18
    # condition). extract_tickers will return [].
    empty_df = pd.DataFrame(
        {
            "Account": ["OTHER"],
            "StockTicker": ["MSFT"],
            "SecurityName": ["MICROSOFT"],
            "Date": ["2026-05-18"],
        }
    )
    monkeypatch.setattr(mod, "fetch", lambda: empty_df)

    rc = mod.main()
    assert rc == 0, "retired path must return 0 (not transient failure)"

    payload = json.loads((state_dir / "watchlist.json").read_text())
    # Source note is the retirement label, NOT the legacy
    # "FFTY-empty+MAG7+..." transient-failover phrasing.
    assert payload["source"] == mod.FFTY_RETIRED_NOTE
    assert "FFTY-empty" not in payload["source"]
    assert "FAILOVER" not in payload["source"].upper()
    # Sizes still reflect the universe truthfully.
    assert payload["sizes"]["ffty"] == 0
    assert payload["sizes"]["total"] >= len(payload["groups"]["mag7"])


def test_zero_ffty_alert_source_label_is_retired_not_failover(_watchlist_module, monkeypatch):
    mod, _ = _watchlist_module
    captured = {}

    def capture_alert(msg, *, source=None, count=None, fallback_used=None):
        captured["source"] = source
        captured["fallback_used"] = fallback_used
        captured["msg"] = msg

    monkeypatch.setattr(mod, "alert_failure", capture_alert)
    monkeypatch.setattr(
        mod,
        "fetch",
        lambda: pd.DataFrame(
            {
                "Account": ["NOT_FFTY"],
                "StockTicker": ["AAA"],
                "SecurityName": ["X"],
                "Date": ["2026-05-18"],
            }
        ),
    )

    mod.main()
    assert captured["source"] == "FFTY-retired"
    assert captured["fallback_used"] is True
    # Don't pretend this is transient anymore.
    assert "FAILOVER" not in captured["msg"].upper()
    assert "retired" in captured["msg"].lower()


def test_fetch_failure_still_uses_distinct_label(_watchlist_module, monkeypatch):
    # Network/HTTP failure is a DIFFERENT condition from the steady-state
    # retirement — its source label must stay distinguishable so an
    # operator scanning alerts can tell the two apart.
    mod, _ = _watchlist_module
    captured = {}

    def capture_alert(msg, *, source=None, count=None, fallback_used=None):
        captured["source"] = source

    monkeypatch.setattr(mod, "alert_failure", capture_alert)

    def boom():
        raise RuntimeError("simulated http 503")

    monkeypatch.setattr(mod, "fetch", boom)
    mod.main()
    assert captured["source"] in (
        "FFTY-fetch-failed-refused",
        "FFTY-fetch-failed-failover",
    )
    # The retired label is reserved for the zero-tickers path.
    assert captured["source"] != "FFTY-retired"


def test_degraded_overwrite_guard_still_engages(_watchlist_module, monkeypatch):
    # Prior watchlist materially larger than the retirement-path size:
    # the existing degraded-overwrite guard must still refuse the write.
    mod, state_dir = _watchlist_module
    # Prior: 100 tickers (above the MIN_PRIOR_FOR_GUARD=80 threshold)
    (state_dir / "watchlist.json").write_text(
        json.dumps(
            {
                "source": "FFTY+MAG7+SP500-top50+NDX100",
                "tickers": [f"T{i:03d}" for i in range(100)],
            }
        )
    )
    # Force the fallback universe size to a small value so guard fires.
    monkeypatch.setattr(mod, "_fallback_universe_size", lambda: 7)
    monkeypatch.setattr(
        mod,
        "fetch",
        lambda: pd.DataFrame(
            {
                "Account": ["OTHER"],
                "StockTicker": ["X"],
                "SecurityName": ["X"],
                "Date": ["2026-05-18"],
            }
        ),
    )

    rc = mod.main()
    assert rc == 1, "guard must refuse the degraded overwrite"
    # Prior watchlist preserved on disk.
    payload = json.loads((state_dir / "watchlist.json").read_text())
    assert len(payload["tickers"]) == 100
