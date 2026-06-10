import datetime as _dt
import os
import sys as _sys
import tempfile
import time as _time
from pathlib import Path

import pandas as pd
import yfinance as yf

_CACHE_READY = False
_BOT_NAME = Path(__file__).resolve().parents[1].name
_CACHE_DIR = Path(
    os.environ.get(
        "YFINANCE_CACHE_DIR",
        str(Path(tempfile.gettempdir()) / "tradingbot_yfinance_cache" / _BOT_NAME),
    )
)


def _clear_dead_proxy() -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        if os.environ.get(key, "").rstrip("/") == "http://127.0.0.1:9":
            os.environ.pop(key, None)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def _ensure_cache() -> None:
    global _CACHE_READY
    if _CACHE_READY:
        return
    _clear_dead_proxy()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Keep yfinance's sqlite caches inside this bot's writable state folder.
    # Otherwise a broken/global cache path makes live scans load 0 symbols.
    try:
        yf.set_tz_cache_location(str(_CACHE_DIR))
    except Exception as e:
        print(f"yf_tz_cache_setup_skipped: {type(e).__name__}: {e}", file=_sys.stderr)
    try:
        yf.cache.set_cache_location(str(_CACHE_DIR))
    except Exception as e:
        print(f"yf_cache_setup_skipped: {type(e).__name__}: {e}", file=_sys.stderr)
    _CACHE_READY = True


def _download_start_end(symbol: str, lookback_days: int) -> pd.DataFrame:
    _ensure_cache()
    end = _dt.date.today()
    start = end - _dt.timedelta(days=int(lookback_days * 1.5) + 30)
    return yf.download(
        symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        progress=False,
        auto_adjust=True,
    )


def fetch_ohlcv(symbol: str, lookback_days: int) -> pd.DataFrame:
    _ensure_cache()
    # yfinance's `period=Nd` silently fails for large N (~2000+ days) on some
    # symbols. Fall back to start/end for long histories so 10-year backtests
    # work reliably.
    #
    # `lookback_days` is a TRADING-day count (callers pass values like 252*5).
    # `timedelta(days=N)` is a CALENDAR-day count. Trading days are ~252/365
    # (≈ 0.69) of calendar days, so to *contain* N trading days we need at
    # least N/0.69 ≈ N*1.45 calendar days. Use *1.5 + 30 buffer for
    # holiday clusters; yfinance trims any extras harmlessly.
    if lookback_days > 1500:
        df = _download_start_end(symbol, lookback_days)
    else:
        # yfinance period mode can return empty for every ticker during API
        # quirks/rate-limit windows. Retry with explicit dates before giving
        # up so live 500-day scans do not go blind.
        df = _normalize(
            yf.download(
                symbol,
                period=f"{lookback_days}d",
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
        )
        if df.empty:
            df = _download_start_end(symbol, lookback_days)
    return _normalize(df)


# T-P1-YF1 — bounded retry + per-symbol telemetry.
#
# The previous backtest fetch loop silently swallowed three different
# failure modes (transient HTTP error, empty response, short-history
# symbol) into one `except Exception: pass`. Four 10-year runs on the
# same commit produced a ~4.5pp CAGR spread driven entirely by yfinance
# silently dropping different symbols on each run.
#
# fetch_ohlcv_diag wraps fetch_ohlcv with a bounded retry budget and
# returns telemetry so callers can log each drop reason explicitly.
# The retry budget is intentionally short (max ~2.5s per failed
# symbol) so a healthy run stays inside the existing time envelope and
# a fully-degraded run gets bounded rather than unbounded slowdown.
RETRY_BACKOFFS_S: tuple[float, ...] = (0.5, 2.0)  # one backoff per retry


def fetch_ohlcv_diag(symbol: str, lookback_days: int) -> tuple[pd.DataFrame, dict]:
    """Like fetch_ohlcv but returns (df, outcome) for visibility.

    outcome keys:
      symbol         the input ticker
      rows_returned  len(df) on the last attempt (may be 0)
      attempts       1 + actual retries taken
      ok             True iff final df is non-empty without exception
      transient      True iff at least one attempt failed before success
      error_class    last exception class name or None
      error_msg      short last error message or None
      elapsed_s      cumulative wall-clock cost across attempts

    Never raises. Caller decides whether ok + rows>=N qualifies the
    symbol for inclusion in the backtest dfs map.
    """
    out: dict = {
        "symbol": symbol,
        "rows_returned": 0,
        "attempts": 0,
        "ok": False,
        "transient": False,
        "error_class": None,
        "error_msg": None,
        "elapsed_s": 0.0,
    }
    t_start = _time.monotonic()
    last_exc: Exception | None = None
    df = pd.DataFrame()
    max_attempts = 1 + len(RETRY_BACKOFFS_S)
    for attempt in range(1, max_attempts + 1):
        out["attempts"] = attempt
        try:
            df = fetch_ohlcv(symbol, lookback_days)
        except Exception as e:
            last_exc = e
            df = pd.DataFrame()
        else:
            if not df.empty:
                last_exc = None
                out["ok"] = True
                out["rows_returned"] = len(df)
                break
            # Empty without exception — treat as transient unless
            # retries exhausted (yfinance returns empty under
            # rate-limit pressure).
            last_exc = RuntimeError("yfinance returned empty frame")
        if attempt <= len(RETRY_BACKOFFS_S):
            backoff = RETRY_BACKOFFS_S[attempt - 1]
            out["transient"] = True
            try:
                print(
                    f"yf_fetch_retry: {symbol} attempt={attempt} "
                    f"backoff={backoff}s "
                    f"err={type(last_exc).__name__}: "
                    f"{str(last_exc)[:80]}",
                    file=_sys.stderr,
                )
            except Exception as log_exc:
                out["error_class"] = out.get("error_class") or type(log_exc).__name__
            _time.sleep(backoff)
    if not out["ok"]:
        out["rows_returned"] = len(df) if not df.empty else 0
        if last_exc is not None:
            out["error_class"] = type(last_exc).__name__
            out["error_msg"] = str(last_exc)[:120] or None
    out["elapsed_s"] = round(_time.monotonic() - t_start, 2)
    return df, out


def classify_drop_reason(outcome: dict, min_rows: int = 250) -> str | None:
    """Return a short human-readable reason a fetch outcome was dropped
    from a backtest universe, or None when the outcome was usable.

    Three distinct classes the operator cares about (T-P1-YF1):
      yf_fetch_failed   - terminal exception after all retries
      yf_short_history  - fetched OK but fewer than min_rows bars
      yf_empty          - all attempts returned empty without exception
    """
    rows = outcome.get("rows_returned", 0) or 0
    if outcome.get("ok") and rows >= min_rows:
        return None
    if outcome.get("ok") and rows < min_rows:
        return f"yf_short_history ({rows} bars)"
    if outcome.get("error_class"):
        msg = (outcome.get("error_msg") or "")[:60]
        return f"yf_fetch_failed ({outcome['error_class']}: {msg})"
    return "yf_empty (all attempts empty)"
