"""Sanity tests for brain/regime.py — deterministic SMA-stack classifier."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from brain.regime import classify, classify_with_hysteresis


def _make_df(n: int, trajectory: str) -> pd.DataFrame:
    """Build a synthetic Close series of length n following trajectory.

    Trajectories tuned so the last 30d return falls in the right band:
      bull        →  ret_30d < 5%   (just trend, no momentum spike)
      bull_run    →  ret_30d >= 5%
      bear        →  ret_30d > -10% (just trend, no crash)
      crash       →  ret_30d <= -10%
    """
    if trajectory == "bull":
        # Sustained uptrend with mild last-30d slope so:
        #   last > sma20 > sma50 > sma200, AND ret_30d < 5%
        fast = 100 + np.arange(n - 30) * 0.5  # 230 bars climb 100 → 214.5
        slow = np.linspace(214.5, 217.4, 30)  # last 30 climb +1.35% (< 5%)
        close = np.concatenate([fast, slow])
    elif trajectory == "bull_run":
        # Mild uptrend then a +10% acceleration in last 30d
        base = 100 + np.arange(n) * 0.2
        close = base.copy()
        close[-30:] *= np.linspace(1.0, 1.10, 30)
    elif trajectory == "bear":
        # Long-term downtrend, last 30d only mildly down → ret_30d ≈ -2%
        downtrend = 200 - np.arange(n - 30) * 0.5
        mild = np.linspace(downtrend[-1], downtrend[-1] * 0.98, 30)
        close = np.concatenate([downtrend, mild])
    elif trajectory == "crash":
        # Mild downtrend then a -15% drop in last 30d
        base = 200 - np.arange(n) * 0.3
        close = base.copy()
        close[-30:] *= np.linspace(1.0, 0.85, 30)
    elif trajectory == "neutral":
        # Healthy pullback: long uptrend, recent 10-day dip below SMA20
        # but everything else still stacked (sma20>sma50>sma200, last>sma200).
        # Required: last < sma20 (so NOT bull) but other gates pass.
        uptrend = 100 + np.arange(n - 10) * 0.5  # 250 bars 100 → 224.5
        pullback = np.full(10, 215.0)  # 10 bars at 215 (4% dip)
        close = np.concatenate([uptrend, pullback])
    elif trajectory == "death_cross_rally":
        # 2022-style: long uptrend that turned, SMA50 below SMA200 now,
        # but a brief rally has price back above SMA200. Should classify
        # as 'bear', NOT 'neutral'.
        close = np.concatenate(
            [
                100 + np.arange(150) * 0.5,  # long uptrend → 174
                np.linspace(174, 130, 80),  # sustained drop → 130
                np.linspace(130, 165, 30),  # rally that pokes above sma200
            ]
        )
    else:
        raise ValueError(trajectory)
    return pd.DataFrame({"Close": close})


def test_bull_run_with_momentum():
    df = _make_df(260, "bull_run")
    assert classify(df) == "bull_run"


def test_bull_steady_uptrend():
    df = _make_df(260, "bull")
    assert classify(df) == "bull"


def test_bear_steady_downtrend():
    df = _make_df(260, "bear")
    assert classify(df) == "bear"


def test_crash_severe_drop():
    df = _make_df(260, "crash")
    assert classify(df) == "crash"


def test_neutral_healthy_pullback():
    # Above SMA200 with golden cross (sma50 > sma200) but price below sma50
    df = _make_df(260, "neutral")
    assert classify(df) == "neutral"


def test_death_cross_rally_classifies_bear_not_neutral():
    # 2022-style: brief rally above SMA200 while SMA50 still below SMA200.
    # Old code returned "neutral" (entries open) → bot bought into bear rallies.
    # Tightened code returns "bear" → entries blocked. THIS IS THE BUG FIX.
    df = _make_df(260, "death_cross_rally")
    assert classify(df) == "bear"


def test_deterministic_same_input_same_output():
    df = _make_df(260, "bull")
    labels = {classify(df) for _ in range(50)}
    assert len(labels) == 1, "classify must be pure"


def test_raises_on_short_series():
    df = pd.DataFrame({"Close": np.arange(100, dtype=float)})
    with pytest.raises(ValueError):
        classify(df)


def test_hysteresis_first_run_commits_immediately(tmp_path):
    state = tmp_path / "regime.json"
    df = _make_df(260, "bull")
    assert classify_with_hysteresis(df, state) == "bull"
    saved = json.loads(state.read_text())
    assert saved["committed"] == "bull"
    assert saved["last_raw"] == "bull"


def test_hysteresis_blocks_single_cycle_flip(tmp_path):
    state = tmp_path / "regime.json"
    bull = _make_df(260, "bull")
    bear = _make_df(260, "bear")

    # Cycle 1: commits to bull
    assert classify_with_hysteresis(bull, state) == "bull"
    # Cycle 2: raw flips to bear, but no prior bear → committed stays bull
    assert classify_with_hysteresis(bear, state) == "bull"
    # Cycle 3: raw still bear; now matches prior raw → commits bear
    assert classify_with_hysteresis(bear, state) == "bear"
