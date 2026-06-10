"""Build the trading watchlist.

Outputs three files into state/:
  - watchlist.json      machine-readable list used by the bot
  - watchlist_ibkr.csv  importable via TWS: File > Import > Watchlist
  - watchlist_finviz.txt comma-separated tickers (paste into Finviz URL)

FFTY status (T-WLIST-FFTY-SOURCE-MIGRATION1, 2026-05-18):
  The original primary universe was FFTY (Innovator IBD-50 ETF),
  fetched by filtering Innovator's combined daily holdings CSV by
  Account == "FFTY". As of 2026-05-18 diagnostics, that combined feed
  no longer contains FFTY rows. The extraction path runs cleanly but
  returns zero tickers.

  This builder treats FFTY as RETIRED from the default path:
    - the combined CSV is still fetched best-effort (so if Innovator
      restores FFTY the universe re-engages automatically)
    - on zero-FFTY-tickers the write proceeds with the MAG7 +
      SP500-top50 + NDX100 universe, and the source_note reflects
      retirement honestly ("MAG7+SP500-top50+NDX100 (FFTY retired
      2026-05-18)") rather than labeling the build as a transient
      failover
    - operators can point at a successor per-fund endpoint without
      a code change via the WATCHLIST_FFTY_URL env var

  Existing safety guards (no-empty-overwrite, degraded-overwrite
  refusal, explicit alert dispatch) are preserved.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import datetime, UTC
from pathlib import Path
from pathlib import Path as _P

import httpx
import pandas as pd

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
from config import MAG7

# WATCHLIST_FFTY_URL lets the operator point the builder at a successor
# per-fund endpoint without code change. Default is Innovator's combined
# holdings CSV (current behavior — preserved for backward compatibility
# and so a future FFTY restoration is automatic).
HOLDINGS_URL = os.environ.get(
    "WATCHLIST_FFTY_URL",
    "https://www.innovatoretfs.com/etf/xt_holdings.csv",
)
FUND = "FFTY"

# Steady-state label when FFTY yields zero tickers from the current
# source. Worded explicitly so dashboards, diagnostic sidecars, and the
# operator's reading of state/watchlist.json all reflect retirement
# rather than transient failover.
FFTY_RETIRED_NOTE = "MAG7+SP500-top50+NDX100 (FFTY retired 2026-05-18)"

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "state"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")


def alert_failure(
    message: str,
    *,
    source: str | None = None,
    count: int | None = None,
    fallback_used: bool | None = None,
) -> None:
    """Notify operator of a watchlist failure.

    Primary path is the formatter-driven WATCHLIST_STALE event
    (T-NOTIFY-WIRE2) so message text is canonical across dashboards /
    diagnostics / archives. Legacy free-form send() path is the bash
    fallback if the formatter import is unavailable for any reason.
    """
    try:
        from notifications.smsbot import send_message
        from notifications.trade_messages import watchlist_stale

        send_message(
            watchlist_stale(
                source=source or "watchlist-build-failure",
                as_of=datetime.now(UTC).date().isoformat(),
                count=count if count is not None else 0,
                fallback_used=fallback_used,
                detail=message,
            )
        )
        return
    except Exception as formatter_exc:
        formatter_error = f"{type(formatter_exc).__name__}: {formatter_exc}"
    try:
        from notifications.smsbot import send

        send(message, "warnings")
    except Exception as e:
        # Best-effort breadcrumb on alert-dispatch failure.  We deliberately
        # do NOT recurse into alert_failure here — if smsbot is down, we
        # would loop. stderr is the only safe channel left.
        print(
            f"alert_failure swallowed: formatter={formatter_error}; sms={type(e).__name__}: {e}",
            file=sys.stderr,
        )


def fetch() -> pd.DataFrame:
    r = httpx.get(HOLDINGS_URL, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    return df


def extract_tickers(df: pd.DataFrame) -> list[str]:
    df = df[df["Account"].astype(str).str.upper() == FUND].copy()
    df["StockTicker"] = df["StockTicker"].astype(str).str.strip().str.upper()
    df["SecurityName"] = df["SecurityName"].astype(str).str.upper()

    # Cast both masks to bool dtype: pandas 4 deprecates `and`-style ops between
    # bool and object Series, which `apply(lambda)` returns by default.
    mask_cash = df["SecurityName"].str.contains("MMDA|MONEY MARKET|CASH", na=False).astype(bool)
    mask_valid = df["StockTicker"].apply(lambda t: bool(TICKER_RE.match(t))).astype(bool)

    tickers = df.loc[~mask_cash & mask_valid, "StockTicker"].dropna().unique().tolist()
    return sorted(tickers)


def _load_sp500_top50() -> list[str]:
    p = OUT_DIR / "sp500_top50.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("tickers", [])
    except (json.JSONDecodeError, KeyError):
        return []


def _load_nasdaq100() -> list[str]:
    p = OUT_DIR / "nasdaq100.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("tickers", [])
    except (json.JSONDecodeError, KeyError):
        return []


# Threshold below which the existing NDX cache is treated as "missing or
# degraded" and an inline rebuild attempt is made. The healthy NDX index has
# ~100 constituents; anything under MIN_NDX is either an unfinished write or
# a stale truncated cache.
MIN_NDX = 90


def _ensure_nasdaq100() -> int:
    """Make sure state/nasdaq100.json is present and non-trivial before the
    failover path uses it as fallback breadth. Returns the count actually
    available afterwards (zero on total failure)."""
    existing = _load_nasdaq100()
    if len(existing) >= MIN_NDX:
        return len(existing)
    # Attempt an inline rebuild via the sibling builder. build_nasdaq100 is
    # self-contained: it tries slickcharts and falls back to a curated
    # ~98-name list, so this call practically never fails. Any exception is
    # caught here so the failover path can still proceed with whatever NDX
    # cache (if any) exists on disk.
    try:
        import importlib

        ndx_mod = importlib.import_module("build_nasdaq100")
        ndx_mod.main()
    except Exception as e:
        print(
            f"watchlist FAILOVER: inline build_nasdaq100 failed ({type(e).__name__}: {e})",
            file=sys.stderr,
        )
    return len(_load_nasdaq100())


def _prior_watchlist_total() -> int:
    """Ticker count in the watchlist.json currently on disk, or 0 if none /
    unreadable. Used by the degraded-fallback guard."""
    p = OUT_DIR / "watchlist.json"
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text()).get("tickers", []) or [])
    except (json.JSONDecodeError, KeyError):
        return 0


# Degraded-fallback guard: if the prior watchlist was at least
# MIN_PRIOR_FOR_GUARD tickers AND a fallback write would shrink the universe
# to less than DEGRADED_RATIO of that, refuse the write and leave the prior
# watchlist on disk. Operator sees the FAIL line and can investigate.
MIN_PRIOR_FOR_GUARD = 80
DEGRADED_RATIO = 0.80


def _fallback_universe_size() -> int:
    """Total unique tickers across the static fallback universe (MAG7 +
    SP500-top50 + NDX100) after a best-effort NDX ensure. This is the size
    a failover write would actually produce."""
    _ensure_nasdaq100()
    return len(set(MAG7) | set(_load_sp500_top50()) | set(_load_nasdaq100()))


def _degraded_overwrite_refused(new_total: int, prior_total: int) -> bool:
    """True if the proposed new_total would be a degraded overwrite of the
    materially larger prior_total. First-time writes (prior_total below
    MIN_PRIOR_FOR_GUARD) are always allowed."""
    if prior_total < MIN_PRIOR_FOR_GUARD:
        return False
    return new_total < int(prior_total * DEGRADED_RATIO)


def write_outputs(
    ffty_tickers: list[str],
    as_of: str,
    source_note: str | None = None,
    *,
    ffty_failed: bool = False,
) -> None:
    sp500_top = _load_sp500_top50()
    ndx100 = _load_nasdaq100()

    all_tickers = sorted(set(ffty_tickers) | set(MAG7) | set(sp500_top) | set(ndx100))

    # T-WATCHLIST-SOURCE-RECOVERY-SIDECAR1 — record source health so the
    # dashboard / release_gate / operator can tell "fresh successful build"
    # from "degraded but preserved last-known-good" from "recovered after
    # an earlier yfinance/upstream hiccup". Read prior_count BEFORE the
    # write (otherwise we'd measure the file we are about to overwrite).
    prior_count = _prior_watchlist_total()
    fetch_failures = (1 if ffty_failed else 0) + (0 if sp500_top else 1) + (0 if ndx100 else 1)
    recovered_from_degraded = bool(
        0 < prior_count < MIN_PRIOR_FOR_GUARD
        and len(all_tickers) >= MIN_PRIOR_FOR_GUARD
        and not ffty_failed
    )
    recovery = {
        "source_count": len(all_tickers),
        "fetch_failures": fetch_failures,
        "ranked_count": len(all_tickers),
        "prior_count": prior_count,
        "recovered_from_degraded": recovered_from_degraded,
    }

    (OUT_DIR / "watchlist.json").write_text(
        json.dumps(
            {
                "source": source_note or f"{FUND}+MAG7+SP500-top50+NDX100",
                "as_of": as_of,
                "tickers": all_tickers,
                "groups": {
                    "ffty": sorted(ffty_tickers),
                    "mag7": MAG7,
                    "sp500_top50": sp500_top,
                    "nasdaq100": ndx100,
                },
                "sizes": {
                    "total": len(all_tickers),
                    "ffty": len(ffty_tickers),
                    "mag7": len(MAG7),
                    "sp500_top50": len(sp500_top),
                    "nasdaq100": len(ndx100),
                },
                "recovery": recovery,
            },
            indent=2,
        )
    )

    lines = ["CSVEXPORT"]
    lines += [f"SYM,{t},SMART/AMEX," for t in all_tickers]
    (OUT_DIR / "watchlist_ibkr.csv").write_text("\n".join(lines) + "\n")

    joined = ",".join(all_tickers)
    (OUT_DIR / "watchlist_finviz.txt").write_text(joined + "\n")
    (OUT_DIR / "watchlist_finviz_url.txt").write_text(
        f"https://finviz.com/screener.ashx?v=111&t={joined}\n"
    )


def main() -> int:
    # T-WATCHLIST-CALLER-PATH-CONSISTENCY1: emit the running script path
    # so cron tails / menu 47 / setup.sh runs all show exactly WHICH
    # builder executed. Distinguishes bot-local from any repo-root copy.
    print(
        f"[build_watchlist] script={__file__} cwd={os.getcwd()}",
        file=sys.stderr,
    )
    try:
        df = fetch()
    except Exception as e:
        fallback_count = _fallback_universe_size()
        prior_total = _prior_watchlist_total()
        if _degraded_overwrite_refused(fallback_count, prior_total):
            msg = (
                f"watchlist FAIL: degraded fallback refused — FFTY fetch failed "
                f"({type(e).__name__}: {e}); fallback would write {fallback_count} "
                f"vs prior {prior_total}. Prior watchlist preserved."
            )
            print(msg, file=sys.stderr)
            alert_failure(
                msg, source="FFTY-fetch-failed-refused", count=prior_total, fallback_used=False
            )
            return 1
        if fallback_count:
            as_of = datetime.now(UTC).date().isoformat()
            msg = (
                f"watchlist FAILOVER: FFTY fetch failed ({type(e).__name__}: {e}); "
                f"writing {fallback_count} fallback tickers from MAG7/SP500/NDX"
            )
            print(msg, file=sys.stderr)
            alert_failure(
                msg, source="FFTY-fetch-failed-failover", count=fallback_count, fallback_used=True
            )
            write_outputs(
                [],
                as_of,
                source_note="FFTY-fetch-failed+MAG7+SP500-top50+NDX100",
                ffty_failed=True,
            )
            return 0
        raise
    as_of = (
        str(df["Date"].iloc[0])
        if "Date" in df.columns and len(df)
        else datetime.now(UTC).date().isoformat()
    )
    tickers = extract_tickers(df)
    if not tickers:
        fallback_count = _fallback_universe_size()
        prior_total = _prior_watchlist_total()
        if _degraded_overwrite_refused(fallback_count, prior_total):
            msg = (
                f"watchlist FAIL: degraded fallback refused — no FFTY tickers "
                f"extracted; fallback would write {fallback_count} vs prior "
                f"{prior_total}. Prior watchlist preserved."
            )
            print(msg, file=sys.stderr)
            alert_failure(msg, source="FFTY-empty-refused", count=prior_total, fallback_used=False)
            return 1
        if fallback_count:
            # FFTY retired from the current source — steady-state write,
            # not a transient failover. Source label and alert text both
            # reflect that. (T-WLIST-FFTY-SOURCE-MIGRATION1)
            msg = (
                "watchlist: FFTY source retired — combined holdings feed "
                f"has no FFTY rows; built {fallback_count} tickers from "
                "MAG7+SP500-top50+NDX100. Set WATCHLIST_FFTY_URL to a "
                "successor per-fund endpoint if one becomes available."
            )
            print(msg, file=sys.stderr)
            alert_failure(msg, source="FFTY-retired", count=fallback_count, fallback_used=True)
            write_outputs([], as_of, source_note=FFTY_RETIRED_NOTE)
            return 0
        msg = "watchlist FAIL: no tickers extracted and no fallback universe available"
        print(msg, file=sys.stderr)
        alert_failure(msg, source="FFTY-empty-no-fallback", count=0, fallback_used=False)
        return 1
    write_outputs(tickers, as_of)

    # Report actual merged totals, not just FFTY
    sp500 = _load_sp500_top50()
    total = len(set(tickers) | set(MAG7) | set(sp500))
    print(
        f"watchlist: {total} unique tickers "
        f"(FFTY {len(tickers)} + MAG7 {len(MAG7)} + SP500 top-{len(sp500)}, as of {as_of})"
    )
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        # Cron tail-grep relies on a single FAIL line to spot silent breakage.
        msg = f"watchlist FAIL: {type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        alert_failure(msg, source="watchlist-build-exception", count=0, fallback_used=False)
        rc = 1
    raise SystemExit(rc)
