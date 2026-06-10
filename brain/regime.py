"""Deterministic SMA-stack market regime classifier.

Replaces brain/hmm_classifier.py — same input contract (pd.DataFrame
of bench OHLC), same output strings (one of REGIME_ORDER below) so
existing config.REGIME_ALLOWED_FOR_ENTRY and call sites work unchanged.

Why deterministic SMA over HMM:
  - HMM with rank-based label assignment permutes labels across refits
    when hidden states have similar means → flicker bull/neutral/bear
    on identical input across consecutive cycles.
  - HMM with covariance_type='full' fails LinAlgError on noisy 500-bar
    SPY windows (covariance loses positive-definiteness).
  - SMA stack is the same logic stage_engine.py already uses for the
    per-stock trend template (gates 1, 4, 5). Benchmark regime should
    use the same methodology — Minervini/IBD already classify market
    via SMA stack ("Stage 1-4"), not statistical regime models.

Hysteresis:
  - Single-day SMA cross-and-back is common. To prevent the new fix
    from re-introducing flicker via a different mechanism, regime only
    commits to the bot's state when the same label appears in two
    consecutive cycles. State persists in state/regime.json.
  - First cycle ever (no prior state) commits immediately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

# Compatible with REGIME_ALLOWED_FOR_ENTRY = {"neutral", "bull", "bull_run"}
REGIME_ORDER = ["crash", "bear", "neutral", "bull", "bull_run"]


def classify(bench_df: pd.DataFrame) -> str:
    """Pure function — same input always returns same label.

    Inputs: DataFrame with 'Close' column, indexed by date, ascending.
    Returns: one of REGIME_ORDER. Raises ValueError on insufficient data.
    """
    close = bench_df["Close"]
    if len(close) < 200:
        raise ValueError(f"need >= 200 bars for SMA200, got {len(close)}")

    last = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    ret_30d = float(close.iloc[-1] / close.iloc[-30] - 1)

    # Strong uptrend with momentum — full stack including SMA20
    if last > sma20 > sma50 > sma200 and ret_30d >= 0.05:
        return "bull_run"
    # Standard uptrend — full stack required (last/SMA20/SMA50/SMA200 all aligned).
    # Adding SMA20 catches early deterioration: SMA20 turning below SMA50 is the
    # earliest signal of momentum loss, ~3-5 weeks before the death cross. This
    # is what HMM was implicitly detecting via volatility-state changes.
    if last > sma20 > sma50 > sma200:
        return "bull"
    # Healthy pullback: above long-term, golden cross active, AND SMA20 still
    # above SMA50 (short-term momentum intact). Filters out rallies during a
    # building bear (where SMA20 has already crossed below SMA50).
    if last > sma200 and sma50 > sma200 and sma20 > sma50:
        return "neutral"
    # Below long-term with severe momentum — capitulation
    if last < sma50 < sma200 and ret_30d <= -0.10:
        return "crash"
    # Otherwise: bear (includes early-deterioration cases where SMA20 < SMA50
    # even before the SMA50/SMA200 death cross fully forms)
    return "bear"


def _commit_hysteresis(raw: str, prior: dict) -> tuple[str, dict]:
    """Pure helper. Apply hysteresis rule given a raw label and prior state.

    Returns (committed_label, new_state). State shape:
      {"last_raw": <raw>, "committed": <committed>}
    """
    last_raw = prior.get("last_raw")
    committed = prior.get("committed")

    # First run, or stale state
    if not committed:
        committed = raw
    # Two consecutive cycles agree → commit the new label
    elif raw == last_raw and raw != committed:
        committed = raw
    # Otherwise keep the committed label (raw differs from committed
    # but doesn't yet agree with prior raw — flicker filter)

    return committed, {"last_raw": raw, "committed": committed}


def classify_with_hysteresis(
    bench_df: pd.DataFrame,
    state_path: Path,
) -> str:
    """File-backed hysteresis. Used by live trading to persist state across
    cron invocations.

    state_path: path to state/regime.json which stores last cycle's raw
    classification + currently-committed label.
    """
    raw = classify(bench_df)

    prior: dict = {}
    if state_path.exists():
        try:
            prior = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            prior = {}

    committed, new_state = _commit_hysteresis(raw, prior)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(new_state, indent=2))

    return committed


def classify_with_state(
    bench_df: pd.DataFrame,
    state: dict | None = None,
) -> tuple[str, dict]:
    """In-memory hysteresis variant. Same logic as classify_with_hysteresis
    but takes/returns state as a dict instead of reading/writing a file.

    Used by the backtest engine where state walks forward in memory across
    the replay loop. Returns (committed_label, new_state); pass new_state
    back in on the next call to chain hysteresis through the loop.
    """
    raw = classify(bench_df)
    return _commit_hysteresis(raw, state or {})
