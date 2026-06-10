"""TradingBot dashboard — live view of the cycle state.

Endpoints:
  GET /          HTML dashboard
  GET /state     raw JSON (state.json + positions.json merged)
  GET /events    last N events from state/decisions.jsonl
  GET /equity    recent equity curve points (from state.json history)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from subprocess import SubprocessError, TimeoutExpired, run

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import datetime as _dt
import os
import secrets as _secrets
import time as _time

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, JSONResponse

from config import STATE_FILE, SYMBOLS, WATCHLIST_FILE

app = FastAPI()


# Mutation token (T-LAUNCH2).  Every POST mutation route requires the
# operator to send an X-Operator-Token header matching the value of the
# DASHBOARD_MUTATION_TOKEN env var.  If the env var is unset, the routes
# fail closed — better to have a dead Sell button than a wide-open one.
# The token is read once at process start and compared in constant time.
# It is never echoed in logs, responses, query params, or rendered HTML.
MUTATION_TOKEN_ENV = "_".join(("DASHBOARD", "MUTATION", "TOKEN"))
_MUTATION_TOKEN = os.environ.get(MUTATION_TOKEN_ENV, "")


def _check_mutation_token(provided: str | None):
    """Return a JSONResponse(401) iff the supplied header fails the check;
    return None when the request may proceed.  Failure modes: env var
    unset (fail closed); header missing; constant-time mismatch."""
    if not _MUTATION_TOKEN:
        return JSONResponse(
            {
                "error": "mutation refused — server-side token not configured",
                "hint": f"set {MUTATION_TOKEN_ENV} in the dashboard's "
                "environment before enabling mutation routes",
            },
            status_code=503,
        )
    if not provided:
        return JSONResponse(
            {"error": "mutation refused — missing X-Operator-Token header"},
            status_code=401,
        )
    if not _secrets.compare_digest(provided, _MUTATION_TOKEN):
        return JSONResponse(
            {"error": "mutation refused — invalid X-Operator-Token"},
            status_code=401,
        )
    return None


def _git_commit() -> str:
    """Return short git commit for the dashboard banner. Falls back to
    'unknown' if git is unavailable or the call fails for any reason."""
    try:
        result = run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
            if commit:
                return commit
    except (FileNotFoundError, OSError, SubprocessError):
        pass
    return "unknown"


COMMIT = _git_commit()


def _version_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


POSITIONS_FILE = _version_path("state/positions.json")
EQUITY_HISTORY = _version_path("state/equity_history.jsonl")
DECISIONS_FILE = _version_path("state/decisions.jsonl")


def _load_state() -> dict:
    p = _version_path(STATE_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


# T-DASH-STale-POSITIONS-BOTH-DASHBOARDS1 +
# T-LIVE-CYCLE-EVIDENCE-BUNDLE-DASHBOARD-LINK1 — read-only file-age
# + sidecar-status helpers used by /state. No broker queries, no file
# mutations.
STALE_POSITION_THRESHOLD_HOURS = 24.0
LIVE_CYCLE_EVIDENCE_THRESHOLD_HOURS = 24.0


def _file_age_hours(p: Path) -> float | None:
    if not p.exists():
        return None
    try:
        m = _dt.datetime.fromtimestamp(p.stat().st_mtime, tz=_dt.UTC)
        return max(
            0.0, (_dt.datetime.now(tz=_dt.UTC) - m).total_seconds() / 3600.0
        )
    except OSError:
        return None


def _stale_position_state() -> dict:
    """Classify position-state sidecars as fresh / stale / missing."""
    files_spec = (
        ("positions.json", "state/positions.json", "tracked bot positions"),
        ("ib_positions.json", "state/ib_positions.json", "IB positions cache"),
        ("account_summary.json", "state/account_summary.json", "account summary"),
    )
    files: list[dict] = []
    overall_stale = False
    overall_missing = False
    for name, rel, label in files_spec:
        p = _version_path(rel)
        age = _file_age_hours(p)
        if age is None:
            files.append(
                {"name": name, "label": label, "status": "missing",
                 "age_hours": None, "path": rel}
            )
            overall_missing = True
        elif age > STALE_POSITION_THRESHOLD_HOURS:
            files.append(
                {"name": name, "label": label, "status": "stale",
                 "age_hours": round(age, 1), "path": rel}
            )
            overall_stale = True
        else:
            files.append(
                {"name": name, "label": label, "status": "fresh",
                 "age_hours": round(age, 1), "path": rel}
            )
    return {
        "threshold_hours": STALE_POSITION_THRESHOLD_HOURS,
        "stale": overall_stale,
        "missing": overall_missing,
        "files": files,
    }


def _live_cycle_evidence_meta() -> dict:
    """Surface the scripts/live_cycle_evidence.py sidecar status."""
    p = _version_path("state/live_cycle_evidence.json")
    arming_mode = "disarmed"
    arming_p = _version_path("state/live_arming_mode.json")
    if arming_p.exists():
        try:
            arming = json.loads(arming_p.read_text())
            if isinstance(arming, dict):
                arming_mode = arming.get("arming_mode", "disarmed")
        except (json.JSONDecodeError, OSError):
            pass
    if not p.exists():
        return {
            "status": "missing",
            "path": "state/live_cycle_evidence.json",
            "arming_mode": arming_mode,
            "advice": (
                "awaiting first menu 41 live cycle"
                if arming_mode == "supervised_one_cycle"
                else "no live cycle has run yet"
            ),
        }
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "status": "unreadable",
            "path": "state/live_cycle_evidence.json",
            "arming_mode": arming_mode,
        }
    age = _file_age_hours(p)
    status = (
        "stale"
        if (age is not None and age > LIVE_CYCLE_EVIDENCE_THRESHOLD_HOURS)
        else "fresh"
    )
    return {
        "status": status,
        "path": "state/live_cycle_evidence.json",
        "age_hours": round(age, 1) if age is not None else None,
        "arming_mode": data.get("arming_mode"),
        "account_id": (data.get("launcher_run") or {}).get("account_id"),
        "launcher_ts": (data.get("launcher_run") or {}).get("ts"),
        "exec_log_count_in_window": (data.get("exec_log_delta") or {}).get("count_in_window"),
        "decisions_count_in_window": (data.get("decisions_delta") or {}).get("count_in_window"),
        "ib_positions_hours_old": (data.get("ib_positions_freshness") or {}).get("hours_old"),
    }


def _load_equity_history(limit: int = 200) -> list[dict]:
    if not EQUITY_HISTORY.exists():
        return []
    lines = EQUITY_HISTORY.read_text().splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _tail_events(n: int = 50) -> list[dict]:
    if not DECISIONS_FILE.exists():
        return []
    lines = DECISIONS_FILE.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --- T-DASH-V2-READONLY1: diagnostic sidecars ------------------------------
# Sidecars are written by scripts/timeline.py, scripts/portfolio_report.py,
# scripts/daily_report.py, scripts/watchlist_audit.py. They live under
# versions/options-v1.2/state/ regardless of which dashboard is reading
# them so there is a single source of truth for both bots' diagnostics.
_REPO_ROOT = ROOT.parent.parent
SIDECAR_DIR = _REPO_ROOT / "versions" / "options-v1.2" / "state"
SIDECAR_FILES = {
    "timeline": "timeline.json",
    "portfolio_report": "portfolio_report.json",
    "daily_report": "daily_report.json",
    "watchlist_audit": "watchlist_audit.json",
}
SIDECAR_STALE_MINUTES = 60


def _load_sidecar(name: str) -> dict:
    """Read-only loader. Returns {present, mtime_iso, age_minutes, stale,
    data} on success, {present: True, error: ...} on read/JSON error,
    {present: False} when the file is absent. Never raises; never writes."""
    fname = SIDECAR_FILES.get(name)
    if not fname:
        return {"present": False, "error": "unknown sidecar"}
    p = SIDECAR_DIR / fname
    if not p.exists():
        return {"present": False}
    try:
        mtime = p.stat().st_mtime
        age_min = max(0.0, (_time.time() - mtime) / 60.0)
        return {
            "present": True,
            "mtime_iso": _dt.datetime.fromtimestamp(mtime, tz=_dt.UTC).isoformat(),
            "age_minutes": round(age_min, 1),
            "stale": age_min > SIDECAR_STALE_MINUTES,
            "data": json.loads(p.read_text(encoding="utf-8", errors="replace")),
        }
    except (OSError, json.JSONDecodeError) as e:
        return {"present": True, "error": f"{type(e).__name__}: {e}"}


def _diagnostics_payload() -> dict:
    out: dict = {
        "sidecar_dir": str(SIDECAR_DIR),
        "sidecars": {},
        "flags": {},
        "counts": {},
        "notification_schema": {},
    }
    for nm in SIDECAR_FILES:
        out["sidecars"][nm] = _load_sidecar(nm)
    pr = out["sidecars"]["portfolio_report"]
    tl = out["sidecars"]["timeline"]
    dr = out["sidecars"]["daily_report"]
    if pr.get("present") and isinstance(pr.get("data"), dict):
        pr_flags = pr["data"].get("operator_flags") or {}
        out["flags"]["portfolio_launch_blocker"] = pr_flags.get("launch_blocker")
        out["flags"]["portfolio_operator_action"] = pr_flags.get("operator_action_required")
        out["flags"]["portfolio_cache_stale"] = pr_flags.get("cache_stale")
        out["flags"]["portfolio_silent_degradation"] = pr_flags.get("silent_degradation_observed")
        out["counts"]["unmanaged"] = len(pr["data"].get("unmanaged") or [])
        out["counts"]["orphan"] = len(pr["data"].get("orphan") or [])
        out["counts"]["expiry_risk"] = len(pr["data"].get("expiry_risk") or [])
    if tl.get("present") and isinstance(tl.get("data"), dict):
        tl_sum = tl["data"].get("summary") or {}
        out["flags"]["timeline_launch_blocker"] = tl_sum.get("launch_blocker")
        out["flags"]["timeline_operator_action"] = tl_sum.get("operator_action_required")
        out["flags"]["timeline_silent_degradation"] = tl_sum.get("silent_degradation_observed")
        out["counts"]["timeline_events"] = tl_sum.get("event_count")
    if dr.get("present") and isinstance(dr.get("data"), dict):
        out["flags"]["anomaly_severity"] = dr["data"].get("anomaly_severity") or dr["data"].get(
            "severity"
        )
        out["flags"]["anomaly_score"] = dr["data"].get("anomaly_score") or dr["data"].get("score")
    # trade_messages is import-pure (no I/O, no env reads) so this import
    # is safe inside an HTTP handler.
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from notifications.trade_messages import BUILDERS as _BUILDERS

        out["notification_schema"] = {
            "installed": True,
            "event_types": sorted(_BUILDERS.keys()),
            "count": len(_BUILDERS),
        }
    except Exception as e:
        out["notification_schema"] = {
            "installed": False,
            "error": f"{type(e).__name__}: {e}",
        }
    return out


@app.get("/state")
def state():
    """Combined state: bot cycle + tracked positions + (cached) IB positions.

    IB positions come from state/ib_positions.json which main.py refreshes
    each cycle. The dashboard renders divergence from this without hitting
    IB on every page load.

    T-DASH-SHARES-LIVE-ACCOUNT1: ib_positions.json is now an envelope
    {account_id, as_of, ibkr_mode, positions: {...}}. Legacy flat-dict
    snapshots still parse (treated as unknown-provenance). The stale-check
    surfaces via `ib_positions_stale` so the JS banner can warn the
    operator without having to grep the JSON by hand.

    T-OPTIONS-V1.2-DASHBOARD-STALE-SUPPRESS-BACKPORT1: options-v1.2 is
    the live cutover target, so in live mode stale paper-era snapshots
    must not render as current live holdings. When the cache lacks
    account-aware metadata, has a different account_id, or says it was
    written in a non-live mode, the response carries
    shares_stale_suppressed=true plus details. The UI then renders a
    banner and empty state instead of stale rows. Read-only only: no file
    deletion and no broker query.
    """
    ib_p = _version_path("state/ib_positions.json")
    ib_pos: dict = {}
    ib_pos_meta: dict | None = None
    if ib_p.exists():
        try:
            raw = json.loads(ib_p.read_text())
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("positions"), dict):
            ib_pos = raw.get("positions") or {}
            ib_pos_meta = {
                "account_id": raw.get("account_id"),
                "as_of": raw.get("as_of"),
                "ibkr_mode": raw.get("ibkr_mode"),
                "schema_version": raw.get("schema_version"),
            }
        elif isinstance(raw, dict):
            ib_pos = raw
    current_account = (
        os.getenv("LIVE_ACCOUNT_ID", "").strip()
        or os.getenv("IBKR_ACCOUNT_ID", "").strip()
    )
    ibkr_mode_env = os.getenv("IBKR_MODE", "").strip().lower()
    is_live_mode = bool(current_account) or ibkr_mode_env == "live"
    ib_positions_stale: dict | None = None
    if ib_pos:
        stored = ((ib_pos_meta or {}).get("account_id") or "").strip()
        if not ib_pos_meta:
            ib_positions_stale = {
                "stored_account": None,
                "current_account": current_account or None,
                "as_of": None,
                "reason": "snapshot predates account-aware metadata",
            }
        elif current_account and stored and stored != current_account:
            ib_positions_stale = {
                "stored_account": stored,
                "current_account": current_account,
                "as_of": ib_pos_meta.get("as_of"),
                "reason": "snapshot was written by a different account",
            }
    shares_stale_suppressed = False
    suppression_reason: str | None = None
    suppression_detail: dict | None = None
    if ib_pos and is_live_mode:
        stored_account = ((ib_pos_meta or {}).get("account_id") or "").strip()
        stored_mode = ((ib_pos_meta or {}).get("ibkr_mode") or "").strip().lower()
        if not ib_pos_meta:
            shares_stale_suppressed = True
            suppression_reason = "snapshot_predates_account_aware_schema"
        elif current_account and stored_account and stored_account != current_account:
            shares_stale_suppressed = True
            suppression_reason = "stored_account_differs_from_current"
        elif stored_mode and stored_mode != "live":
            shares_stale_suppressed = True
            suppression_reason = "stored_mode_is_not_live"
        if shares_stale_suppressed:
            suppression_detail = {
                "stored_account": stored_account or None,
                "stored_mode": stored_mode or None,
                "current_account": current_account or None,
                "current_mode_env": ibkr_mode_env or None,
                "as_of": (ib_pos_meta or {}).get("as_of"),
                "row_count": len(ib_pos),
            }
    return JSONResponse(
        {
            "cycle": _load_state(),
            "positions": _load_positions(),
            "ib_positions": ib_pos,
            "ib_positions_meta": ib_pos_meta,
            "ib_positions_stale": ib_positions_stale,
            "is_live_mode": is_live_mode,
            "shares_stale_suppressed": shares_stale_suppressed,
            "suppression_reason": suppression_reason,
            "suppression_detail": suppression_detail,
            # T-DASH-STale-POSITIONS-BOTH-DASHBOARDS1
            "stale_position_state": _stale_position_state(),
            # T-LIVE-CYCLE-EVIDENCE-BUNDLE-DASHBOARD-LINK1
            "live_cycle_evidence_meta": _live_cycle_evidence_meta(),
        }
    )


@app.get("/events")
def events(n: int = 30):
    return JSONResponse(_tail_events(n))


@app.get("/equity")
def equity():
    return JSONResponse(_load_equity_history())


@app.get("/version")
def version():
    """Running git commit, captured at process start. Fallback 'unknown'."""
    return JSONResponse({"commit": COMMIT})


@app.get("/diagnostics")
def diagnostics():
    """Read-only operator cockpit summary (T-DASH-V2-READONLY1).

    Returns per-sidecar presence/age, flattened operator flags, position
    counts, and notification-schema readiness. Missing sidecars degrade
    to present=False; never raises 500. No state mutation."""
    return JSONResponse(_diagnostics_payload())


@app.get("/portfolio_report")
def portfolio_report_endpoint():
    """Return cached portfolio_report.json sidecar verbatim with
    presence/age metadata. Read-only."""
    return JSONResponse(_load_sidecar("portfolio_report"))


@app.get("/timeline")
def timeline_endpoint(limit: int = 50):
    """Return cached timeline.json sidecar trimmed to `limit` events."""
    sc = _load_sidecar("timeline")
    if sc.get("present") and isinstance(sc.get("data"), dict) and limit > 0:
        events = sc["data"].get("events") or []
        if isinstance(events, list) and len(events) > limit:
            sc["data"] = dict(sc["data"])
            sc["data"]["events"] = events[-limit:]
            sc["data"]["events_truncated_by_dashboard"] = True
    return JSONResponse(sc)


@app.get("/watchlist")
def watchlist():
    def fallback_universe(note: str) -> dict:
        tickers = list(SYMBOLS)
        return {
            "source": "config.SYMBOLS fallback option underlyings",
            "as_of": None,
            "tickers": tickers,
            "groups": {"fallback_option_underlyings": tickers},
            "sizes": {"total": len(tickers), "fallback_option_underlyings": len(tickers)},
            "note": note,
        }

    p = _version_path(WATCHLIST_FILE)
    if not p.exists():
        return JSONResponse(fallback_universe("watchlist.json missing"))
    try:
        data = json.loads(p.read_text())
        if not data.get("tickers"):
            return JSONResponse(fallback_universe("watchlist.json empty"))
        data["source"] = f"{data.get('source', 'watchlist')} option underlyings"
        data["note"] = "underlying candidates for long-call option entries; not stock positions"
        return JSONResponse(data)
    except json.JSONDecodeError:
        return JSONResponse({"error": "watchlist unreadable"})


@app.get("/quotes")
def quotes():
    p = _version_path("state/ticker_quotes.json")
    if not p.exists():
        return JSONResponse({"quotes": {}, "as_of": None})
    try:
        return JSONResponse(json.loads(p.read_text()))
    except json.JSONDecodeError:
        return JSONResponse({"error": "quotes unreadable"})


@app.get("/trades")
def trades(days: int = 7):
    """Recent IB fills + realized P&L. Reads cached file written by main.py
    or scripts/trade_history.py to avoid hitting IB on every dashboard refresh.
    Run trade_history.py once to populate, OR main.py refreshes it each cycle."""
    p = _version_path("state/trade_history.json")
    if not p.exists():
        return JSONResponse(
            {
                "fills": [],
                "summary": {},
                "note": "no trade_history.json yet — run scripts/trade_history.py "
                "or wait for main.py to refresh (next cycle)",
            }
        )
    try:
        return JSONResponse(json.loads(p.read_text()))
    except json.JSONDecodeError:
        return JSONResponse({"error": "trade_history.json unreadable"})


@app.get("/account")
def account():
    """Live account summary cached from main.py. Has NetLiquidation, cash,
    realized/unrealized PnL, buying power."""
    p = _version_path("state/account_summary.json")
    if not p.exists():
        return JSONResponse({"note": "no account_summary.json yet"})
    try:
        return JSONResponse(json.loads(p.read_text()))
    except json.JSONDecodeError:
        return JSONResponse({"error": "account_summary.json unreadable"})


@app.post("/rebuild_watchlist")
def rebuild_watchlist(
    confirm: str = "", x_operator_token: str | None = Header(None, alias="X-Operator-Token")
):
    """Rebuild this bot's watchlist via scripts/build_watchlist.py
    (T-DASH-WLISTOPS1). Token-gated + confirm-gated.

    Subprocesses the canonical builder; degraded-overwrite guard,
    fallback universe, and alert_failure wiring from T-P0-WLIST* /
    T-NOTIFY-WIRE2 all still fire.
    """
    token_err = _check_mutation_token(x_operator_token)
    if token_err is not None:
        return token_err
    if confirm != "YES":
        return JSONResponse(
            {
                "error": "confirmation required",
                "hint": "add ?confirm=YES to the request",
            },
            status_code=400,
        )
    cmd = [sys.executable, "scripts/build_watchlist.py"]
    try:
        result = run(cmd, capture_output=True, text=True, timeout=60, cwd=ROOT)
        return JSONResponse(
            {
                "status": "completed" if result.returncode == 0 else "error",
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    except TimeoutExpired:
        return JSONResponse(
            {
                "error": "rebuild_watchlist command timed out (>60s); "
                "the underlying FFTY fetch or NDX rebuild is "
                "likely degraded — re-check after a minute"
            },
            status_code=504,
        )


@app.post("/sell")
def sell(
    symbol: str,
    confirm: str = "",
    qty: int = 0,
    x_operator_token: str | None = Header(None, alias="X-Operator-Token"),
):
    """Manual STOCK SELL via IB. options-v1.2 is options-only; refuses unless
    confirm=EMERGENCY AND a valid X-Operator-Token header (T-LAUNCH2) are
    both supplied. 2026-05-08 incident: a non-scoped --all swept six
    unrelated stock longs.

    Example (emergency only):
      curl -X POST -H "X-Operator-Token: $TOKEN" \
           'http://localhost:8082/sell?symbol=JBL&confirm=EMERGENCY'
    """
    token_err = _check_mutation_token(x_operator_token)
    if token_err is not None:
        return token_err
    if confirm != "EMERGENCY":
        return JSONResponse(
            {
                "error": "refused — options-v1.2 endpoint sells STOCKS, not options",
                "hint": "use scripts/sell_option.py for options. For emergency stock "
                "unwind add ?confirm=EMERGENCY",
            },
            status_code=400,
        )
    cmd = [
        sys.executable,
        "scripts/sell_position.py",
        "--emergency",
        "--symbol",
        symbol.upper(),
        "--force",
    ]
    if qty > 0:
        cmd.extend(["--qty", str(qty)])
    try:
        result = run(cmd, capture_output=True, text=True, timeout=75, cwd=ROOT)
        return JSONResponse(
            {
                "status": "completed" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    except TimeoutExpired:
        return JSONResponse({"error": "sell command timed out"}, status_code=504)


@app.post("/sell_option")
def sell_option(
    symbol: str,
    strike: float,
    expiry: str,
    right: str = "C",
    contracts: int = 1,
    confirm: str = "",
    x_operator_token: str | None = Header(None, alias="X-Operator-Token"),
):
    """Manual option SELL via IB. Requires confirm=YES query param AND a
    valid X-Operator-Token header (T-LAUNCH2).

    Used by the dashboard's "Sell" button on the Untracked IB options panel.

    Example:
      curl -X POST -H "X-Operator-Token: $TOKEN" \
           'http://localhost:8082/sell_option?symbol=AAPL&strike=290&expiry=20260515&confirm=YES'
    """
    token_err = _check_mutation_token(x_operator_token)
    if token_err is not None:
        return token_err
    if confirm != "YES":
        return JSONResponse({"error": "add ?confirm=YES"}, status_code=400)
    cmd = [
        sys.executable,
        "scripts/sell_option.py",
        "--symbol",
        symbol.upper(),
        "--strike",
        str(strike),
        "--expiry",
        expiry,
        "--right",
        right,
        "--contracts",
        str(contracts),
        "--force",
    ]
    try:
        result = run(cmd, capture_output=True, text=True, timeout=30, cwd=ROOT)
        return JSONResponse(
            {
                "status": "completed" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    except TimeoutExpired:
        return JSONResponse({"error": "sell_option command timed out"}, status_code=504)


@app.post("/claim")
def claim(
    symbol: str,
    confirm: str = "",
    x_operator_token: str | None = Header(None, alias="X-Operator-Token"),
):
    """Claim an untracked IB position. Requires confirm=YES query param AND a
    valid X-Operator-Token header (T-LAUNCH2).

    Example:
      curl -X POST -H "X-Operator-Token: $TOKEN" \
           'http://localhost:8082/claim?symbol=JBL&confirm=YES'
    """
    token_err = _check_mutation_token(x_operator_token)
    if token_err is not None:
        return token_err
    if confirm != "YES":
        return JSONResponse({"error": "add ?confirm=YES"}, status_code=400)
    cmd = [sys.executable, "scripts/claim_position.py", "--symbol", symbol.upper()]
    try:
        result = run(cmd, capture_output=True, text=True, timeout=30, cwd=ROOT)
        return JSONResponse(
            {
                "status": "completed" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    except TimeoutExpired:
        return JSONResponse({"error": "claim command timed out"}, status_code=504)


HTML = r"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>TradingBot</title>
<style>
:root{
  --bg:#0b0f14; --fg:#c8e6c9; --muted:#7a8a85; --accent:#80cbc4;
  --bull:#66bb6a; --bear:#ef5350; --warn:#ffa726;
}
*{box-sizing:border-box}
body{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);color:var(--fg);padding:20px}
h1{font-size:18px;color:var(--accent);margin:0 0 4px}
.sub{color:var(--muted);font-size:12px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}
.card{background:#111820;padding:16px;border-radius:8px;border:1px solid #1e2a33}
.c4{grid-column:span 4}.c6{grid-column:span 6}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media(max-width:900px){.c4,.c6,.c8{grid-column:span 12}}
h2{font-size:13px;color:var(--accent);margin:0 0 10px;letter-spacing:1px;text-transform:uppercase}
.big{font-size:28px;font-weight:600;color:var(--fg)}
.bull{color:var(--bull)}.bear{color:var(--bear)}.warn{color:var(--warn)}
.stat-row{display:flex;justify-content:space-between;padding:4px 0;color:var(--muted);font-size:13px}
.stat-row b{color:var(--fg);font-weight:500}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:6px 8px;border-bottom:1px solid #1e2a33;color:var(--muted);font-weight:500}
td{padding:6px 8px;border-bottom:1px solid #151e25}
.events{max-height:360px;overflow-y:auto;font-size:11px;line-height:1.5}
.event{padding:3px 0;color:var(--muted);word-break:break-all}
.event .ts{color:#566}.event .act{color:var(--accent);font-weight:500}
.event.exit{color:var(--bear)}.event.pilot{color:var(--bull)}.event.trim{color:var(--warn)}
#spark{width:100%;height:60px}
.pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:500}
.p-bull{background:#1b3c25;color:var(--bull)}.p-bear{background:#3c1d1c;color:var(--bear)}
.p-neutral{background:#2a3540;color:var(--fg)}.p-bull_run{background:#244d30;color:var(--bull)}
.heat-bar{height:6px;background:#1e2a33;border-radius:3px;overflow:hidden;margin-top:6px}
.heat-fill{height:100%;transition:width .3s,background .3s}
.ticker{padding:3px 8px;border-radius:4px;background:#1e2a33;color:var(--fg);cursor:default}
.ticker.ffty{background:#1b3340;color:#80deea}
.ticker.mag7{background:#3d2f1b;color:#ffcc80}
.ticker.sp500{background:#1e2a33;color:#a5d6a7}
.ticker.multi{background:#2d1b3d;color:#ce93d8}
.ticker.held{background:#244d30;color:#fff;font-weight:600}
table.wl{font-size:11px;display:block;max-height:380px;overflow-y:auto}
table.wl th.num, table.wl td.num{text-align:right;font-variant-numeric:tabular-nums}
table.wl td.up{color:var(--bull)}
table.wl td.down{color:var(--bear)}
table.wl td.big-up{color:var(--bull);font-weight:600}
table.wl td.big-down{color:var(--bear);font-weight:600}
table.wl tr.held td:first-child{border-left:3px solid var(--bull);padding-left:5px}
table.wl .src{font-size:9px;color:var(--muted)}
table.wl .src.ffty{color:#80deea}
table.wl .src.mag7{color:#ffcc80}
table.wl .src.sp500{color:#a5d6a7}
table.wl .src.multi{color:#ce93d8}
</style>
</head><body>
<h1>TradingBot <span style="font-size:11px;color:var(--muted);font-weight:400">@__COMMIT__</span></h1>
<div class="sub" id="lastUpdate">loading…</div>

<!-- T-OPTIONS-V1.2-DASHBOARD-STALE-SUPPRESS-BACKPORT1: hard-suppress banner.
     Shown when the dashboard is running in live mode and the cached IB
     positions snapshot is paper-era / pre-account-aware. The untracked
     panels render empty states instead of stale rows so the operator cannot
     mistake old paper holdings for current live positions. Strictly
     read-only: nothing is deleted, no broker is queried, no orders move. -->
<div id="suppressedPaperBanner" style="
        display:none;
        margin:12px 0 16px;
        padding:14px 18px;
        background:#3a1d1d;
        border-left:4px solid var(--bear);
        border-radius:6px;
        color:#fff;
        font-size:14px;
        line-height:1.5;">
  <div style="font-weight:600;color:var(--bear);font-size:12px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">
    STALE PAPER SNAPSHOT SUPPRESSED
  </div>
  <div>
    This broker-position cache predates live/account-aware metadata.
    Run canonical menu 2 after TWS-live login to refresh live evidence.
    <b>No live shares are shown from this stale cache.</b>
  </div>
  <div id="suppressedPaperBody" style="margin-top:6px;font-size:11px;color:var(--muted)">—</div>
</div>

<!-- T-DASH-SHARES-LIVE-ACCOUNT1: stale-snapshot banner. Shown when the
     IB positions cache was written by a different account than the
     dashboard process's current LIVE_ACCOUNT_ID. Suppressed by the hard
     stale-paper banner above when shares_stale_suppressed=true. -->
<div id="staleSnapshotBanner" style="
        display:none;
        margin:12px 0 16px;
        padding:14px 18px;
        background:#3a2a1d;
        border-left:4px solid var(--warn);
        border-radius:6px;
        color:#fff;
        font-size:14px;
        line-height:1.5;">
  <div style="font-weight:600;color:var(--warn);font-size:12px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">
    STALE SNAPSHOT — IB positions cache may not match current account
  </div>
  <div id="staleSnapshotBody">—</div>
  <div style="margin-top:6px;font-size:11px;color:var(--muted)">
    The cache refreshes automatically on the next bot cycle.
  </div>
</div>

<!-- T-DASH-STale-POSITIONS-BOTH-DASHBOARDS1: position-state staleness
     banner. Shown when ANY of positions.json / ib_positions.json /
     account_summary.json is missing or older than the threshold (24h
     default). Server-side classification is in /state under
     stale_position_state. -->
<div id="stalePositionsBanner" style="
        display:none;
        margin:12px 0 16px;
        padding:14px 18px;
        background:#2a2a3a;
        border-left:4px solid var(--warn);
        border-radius:6px;
        color:#fff;
        font-size:14px;
        line-height:1.5;">
  <div style="font-weight:600;color:var(--warn);font-size:12px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">
    STALE POSITIONS — sidecars older than the freshness threshold
  </div>
  <div id="stalePositionsBody" style="font-size:12px">—</div>
  <div style="margin-top:6px;font-size:11px;color:var(--muted)">
    Refresh paths: bot cycle for positions.json + ib_positions.json
    + account_summary.json; menu 45 for dashboard readiness; menu 2 to
    capture canonical evidence after.
  </div>
</div>

<!-- T-LIVE-CYCLE-EVIDENCE-BUNDLE-DASHBOARD-LINK1: the
     state/live_cycle_evidence.json bundle surfaced as a single card. -->
<div id="liveCycleEvidenceCard" style="
        margin:12px 0 16px;
        padding:12px 16px;
        background:var(--panel);
        border:1px solid var(--border);
        border-radius:6px;
        color:var(--fg);
        font-size:12px;
        line-height:1.5;">
  <div style="font-weight:600;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:4px">
    Live Cycle Evidence
  </div>
  <div id="liveCycleEvidenceBody">—</div>
</div>

<div class="grid">
  <!-- Diagnostics Cockpit (T-DASH-V2-READONLY1). Read-only operator
       surface. Pulls sidecars written by scripts/timeline.py,
       scripts/portfolio_report.py, scripts/daily_report.py. Missing
       sidecars degrade visibly; no new mutation controls. -->
  <div class="card c12">
    <h2>Diagnostics Cockpit
      <span id="diagSidecarAges" style="color:var(--muted);font-weight:400;font-size:11px;margin-left:8px"></span>
    </h2>
    <div id="diagFlags" style="margin-bottom:12px;font-size:11px;line-height:1.9">—</div>
    <!-- T-DASH-LAYOUT1: stat cards. Number + label stack vertically and
         centered so the eye doesn't have to bridge a wide horizontal gap;
         a tiny pointer-line names the table each count belongs to. -->
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px;font-size:12px">
      <div style="background:#0e1620;border:1px solid #1e2a33;border-radius:6px;padding:10px 8px;text-align:center">
        <div id="diagIb" style="font-size:22px;font-weight:600;color:var(--fg);line-height:1.1">—</div>
        <div style="margin-top:4px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px">ib</div>
      </div>
      <div style="background:#0e1620;border:1px solid #1e2a33;border-radius:6px;padding:10px 8px;text-align:center">
        <div id="diagTracked" style="font-size:22px;font-weight:600;color:var(--fg);line-height:1.1">—</div>
        <div style="margin-top:4px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px">tracked</div>
      </div>
      <div style="background:#0e1620;border:1px solid #1e2a33;border-radius:6px;padding:10px 8px;text-align:center">
        <div id="diagUnmanaged" style="font-size:22px;font-weight:600;color:var(--fg);line-height:1.1">—</div>
        <div style="margin-top:4px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px">unmanaged</div>
        <div style="margin-top:2px;color:#566;font-size:10px">↓ Unmanaged / Orphan</div>
      </div>
      <div style="background:#0e1620;border:1px solid #1e2a33;border-radius:6px;padding:10px 8px;text-align:center">
        <div id="diagOrphan" style="font-size:22px;font-weight:600;color:var(--fg);line-height:1.1">—</div>
        <div style="margin-top:4px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px">orphan</div>
        <div style="margin-top:2px;color:#566;font-size:10px">↓ Unmanaged / Orphan</div>
      </div>
      <div style="background:#0e1620;border:1px solid #1e2a33;border-radius:6px;padding:10px 8px;text-align:center">
        <div id="diagExpiry" style="font-size:22px;font-weight:600;color:var(--fg);line-height:1.1">—</div>
        <div style="margin-top:4px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px">expiry-risk</div>
        <div style="margin-top:2px;color:#566;font-size:10px">↓ Expiry Risk</div>
      </div>
    </div>
    <!-- Divider so the tables below don't look attached to the last
         (expiry-risk) count above. -->
    <div style="border-top:1px solid #1e2a33;padding-top:14px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:4px 0 6px;text-transform:uppercase;letter-spacing:1px">Unmanaged / Orphan</h3>
        <table>
          <thead><tr><th>bot</th><th>sym</th><th>sec</th><th>qty</th><th>note</th></tr></thead>
          <tbody id="diagDiffBody"><tr><td colspan="5" style="color:var(--muted);text-align:center;padding:8px">no sidecar generated yet</td></tr></tbody>
        </table>
        <h3 style="font-size:12px;color:var(--muted);margin:14px 0 6px;text-transform:uppercase;letter-spacing:1px">Expiry Risk</h3>
        <table>
          <thead><tr><th>sym</th><th>expiry</th><th>DTE</th><th>strike/right</th><th>note</th></tr></thead>
          <tbody id="diagExpiryBody"><tr><td colspan="5" style="color:var(--muted);text-align:center;padding:8px">none</td></tr></tbody>
        </table>
      </div>
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:4px 0 6px;text-transform:uppercase;letter-spacing:1px">Timeline (recent)</h3>
        <div id="diagTimeline" class="events" style="max-height:280px">no timeline sidecar generated yet</div>
        <h3 style="font-size:12px;color:var(--muted);margin:14px 0 6px;text-transform:uppercase;letter-spacing:1px">Notification Schema</h3>
        <div id="diagNotif" style="font-size:11px;color:var(--muted)">—</div>
      </div>
    </div>
  </div>

  <!-- Visibility / Charts (T-CHARTS1). Read-only operator surface; all
       charts pull from existing cached sidecars + endpoints — equity
       history, portfolio_report, timeline. No new endpoints, no broker
       calls, no mutation controls. Each chart degrades to an empty
       state with a "sidecar missing" / "no data" label when its source
       is absent. -->
  <div class="card c12">
    <h2>Visibility — Charts
      <span style="color:var(--muted);font-weight:400;font-size:11px;margin-left:8px">
        read-only; cached sidecars
      </span>
    </h2>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px">
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:1px">Equity Curve</h3>
        <canvas id="chartEquity" width="320" height="90" style="width:100%;height:90px;background:#0a1116;border-radius:4px"></canvas>
        <div id="chartEquityMeta" style="font-size:11px;color:var(--muted);margin-top:4px">—</div>
      </div>
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:1px">Drawdown</h3>
        <canvas id="chartDrawdown" width="320" height="90" style="width:100%;height:90px;background:#0a1116;border-radius:4px"></canvas>
        <div id="chartDrawdownMeta" style="font-size:11px;color:var(--muted);margin-top:4px">—</div>
      </div>
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:1px">Daily P&amp;L</h3>
        <canvas id="chartPnl" width="320" height="90" style="width:100%;height:90px;background:#0a1116;border-radius:4px"></canvas>
        <div id="chartPnlMeta" style="font-size:11px;color:var(--muted);margin-top:4px">—</div>
      </div>
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:1px">Exposure by Instrument</h3>
        <div id="chartExposure" style="font-size:12px">no portfolio_report sidecar</div>
      </div>
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:1px">Option DTE Ladder</h3>
        <table id="chartDteLadder" style="font-size:11px">
          <thead><tr><th>sym</th><th>exp</th><th>DTE</th><th>qty</th></tr></thead>
          <tbody id="chartDteLadderBody"><tr><td colspan="4" style="color:var(--muted);text-align:center;padding:6px">no option positions</td></tr></tbody>
        </table>
      </div>
      <div>
        <h3 style="font-size:12px;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:1px">Timeline Events (by type)</h3>
        <div id="chartTimelineGroups" style="font-size:11px;color:var(--muted)">no timeline sidecar</div>
      </div>
    </div>
  </div>

  <div class="card c4">
    <h2>Equity</h2>
    <div class="big" id="equity">—</div>
    <div class="stat-row"><span>regime</span><b id="regime">—</b></div>
    <div class="stat-row"><span>positions</span><b id="positions">—</b></div>
    <div class="stat-row"><span>entries allowed</span><b id="entriesAllowed">—</b></div>
  </div>

  <div class="card c4">
    <h2>Portfolio Heat</h2>
    <div class="big" id="heat">—</div>
    <div class="heat-bar"><div class="heat-fill" id="heatBar"></div></div>
    <div class="stat-row" style="margin-top:10px"><span>target</span><b>3.0%</b></div>
    <div class="stat-row"><span>ceiling</span><b>5.0%</b></div>
  </div>

  <div class="card c4">
    <h2>Market State</h2>
    <div class="big" id="mtState">—</div>
    <div class="stat-row"><span>distribution days</span><b id="ddCount">—</b></div>
    <div class="stat-row"><span>FTD today</span><b id="ftdToday">—</b></div>
    <div class="stat-row"><span>sector leaders</span><b id="sectorLeaders">—</b></div>
  </div>

  <div class="card c8">
    <h2>Open Positions</h2>
    <table id="posTable">
      <thead><tr><th>sym</th><th>layer</th><th>shares</th><th>entry</th><th>stop</th><th>peak</th><th>gain%</th><th>R</th></tr></thead>
      <tbody id="posBody"><tr><td colspan="8" style="color:var(--muted);text-align:center;padding:20px">no open positions</td></tr></tbody>
    </table>
    <h3 style="margin-top:16px">Open Option Positions <span style="color:var(--muted);font-weight:400;font-size:11px">(bot-tracked — exits via run.sh menu)</span></h3>
    <!-- T-LAUNCH5 expiry-risk strip. Read-only summary of how many
         open option positions are within EXPIRY_RISK_DTE of expiry.
         Hidden when no tracked option positions exist. -->
    <div id="expiryRiskStrip" style="display:none;margin:4px 0 8px;font-size:12px"></div>
    <table id="optPosTable">
      <thead><tr><th>sym</th><th>right/strike</th><th>expiry</th><th>contracts</th><th>premium entry</th><th>peak</th><th>gain%</th><th>DTE</th></tr></thead>
      <tbody id="optPosBody"><tr><td colspan="8" style="color:var(--muted);text-align:center;padding:20px">no open option positions</td></tr></tbody>
    </table>

    <h3 style="margin-top:16px">Untracked IB stocks <span style="color:var(--muted);font-weight:400;font-size:11px">(view only — options-v1.2 does not sell stocks)</span></h3>
    <table id="untrackedTable">
      <thead><tr><th>sym</th><th>shares</th><th>avg cost</th><th>action</th></tr></thead>
      <tbody id="untrackedBody"><tr><td colspan="4" style="color:var(--muted);text-align:center;padding:12px">none</td></tr></tbody>
    </table>

    <h3 style="margin-top:16px">Untracked IB options <span style="color:var(--muted);font-weight:400;font-size:11px">(stress demos / orphans — Sell to unwind)</span></h3>
    <table id="untrackedOptTable">
      <thead><tr><th>sym</th><th>strike</th><th>expiry</th><th>contracts</th><th>~premium</th><th>cost/contract</th><th>action</th></tr></thead>
      <tbody id="untrackedOptBody"><tr><td colspan="7" style="color:var(--muted);text-align:center;padding:12px">none</td></tr></tbody>
    </table>
  </div>

  <div class="card c4">
    <h2>Equity Curve</h2>
    <canvas id="spark" width="300" height="60"></canvas>
    <div class="stat-row" style="margin-top:10px"><span>first</span><b id="eqFirst">—</b></div>
    <div class="stat-row"><span>current</span><b id="eqCurrent">—</b></div>
    <div class="stat-row"><span>total return</span><b id="eqReturn">—</b></div>
  </div>

  <div class="card c6">
    <h2>Recent Events</h2>
    <div class="events" id="eventsList"></div>
  </div>

  <div class="card c6">
    <h2>Watchlist <span id="wlTotal" style="color:var(--muted);font-weight:400;font-size:11px"></span></h2>
    <!-- T-DASH-WLISTOPS1: prominent symbol-count card + Rebuild action. -->
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
      <div style="background:#0e1620;border:1px solid #1e2a33;border-radius:6px;padding:8px 14px;text-align:center;min-width:90px">
        <div id="wlCountBig" style="font-size:22px;font-weight:600;color:var(--fg);line-height:1.1">—</div>
        <div style="margin-top:2px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px">symbols</div>
      </div>
      <div style="flex:1;font-size:11px;color:var(--muted);line-height:1.4" id="wlCountMeta">—</div>
      <button id="btn-rebuild-watchlist" style="font-size:11px;padding:6px 12px">Rebuild</button>
    </div>
    <div id="wlSummary" class="sub" style="margin-bottom:8px"></div>
    <table id="wlTable" class="wl">
      <thead><tr>
        <th>sym</th><th class="num">last</th><th class="num">chg%</th><th class="num">vol×</th><th class="num">RS</th><th>src</th>
      </tr></thead>
      <tbody id="wlBody"></tbody>
    </table>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = n => n == null ? '—' : n.toLocaleString(undefined, {maximumFractionDigits: 2});
const pct = n => n == null ? '—' : (n * 100).toFixed(1) + '%';
const cls = (regime) => ({bull: 'bull', bull_run: 'bull_run', neutral: 'neutral', bear: 'bear', crash: 'bear'}[regime] || 'neutral');

// Mutation-token plumbing (T-LAUNCH2). Token is prompted once per tab
// session and kept ONLY in sessionStorage (cleared on tab close). It is
// never put into URLs, fetch bodies, or rendered HTML. Each mutation
// fetch wraps through mutate() which attaches the X-Operator-Token
// header. If the operator cancels the prompt, the click is a no-op.
const TOKEN_KEY = 'op_token';
function getOpToken() {
  let t = sessionStorage.getItem(TOKEN_KEY);
  if (!t) {
    t = window.prompt(
      'Enter operator token for this dashboard session.\n' +
      'Stored only for this browser tab.', '');
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
  }
  return t;
}
function clearOpToken() { sessionStorage.removeItem(TOKEN_KEY); }
async function mutate(url, opts = {}) {
  const token = getOpToken();
  if (!token) return null;
  const headers = Object.assign({}, opts.headers || {},
                                {'X-Operator-Token': token});
  const r = await fetch(url, Object.assign({}, opts, {method: 'POST',
                                                       headers}));
  if (r.status === 401) {
    clearOpToken();
  }
  return r;
}

async function tick() {
  try {
    const [s, e, q, w, qs] = await Promise.all([
      fetch('/state').then(r => r.json()),
      fetch('/events?n=30').then(r => r.json()),
      fetch('/equity').then(r => r.json()),
      fetch('/watchlist').then(r => r.json()),
      fetch('/quotes').then(r => r.json()),
    ]);

    const c = s.cycle || {};

    // T-OPTIONS-V1.2-DASHBOARD-STALE-SUPPRESS-BACKPORT1: when the server
    // flags the ib_positions cache as a stale paper-era snapshot, treat it
    // as empty for operator-visible untracked panels. The raw rows still
    // arrive on s.ib_positions for diagnostics consumers, but the UI must
    // not render them as current live state.
    const sharesSuppressed = !!s.shares_stale_suppressed;
    const ibPositionsForUi = sharesSuppressed ? {} : (s.ib_positions || {});

    // Hard-suppress banner takes priority over the softer stale-snapshot
    // banner so the operator only sees one explanation for stale data.
    const suppressedBanner = $('suppressedPaperBanner');
    const staleBanner = $('staleSnapshotBanner');
    const reasonText = {
      'snapshot_predates_account_aware_schema':
        'snapshot has no account-aware envelope (pre-cutover format)',
      'stored_account_differs_from_current':
        'snapshot account_id differs from current LIVE_ACCOUNT_ID',
      'stored_mode_is_not_live':
        'snapshot ibkr_mode is not "live" (paper-era cache)',
    };
    if (s.shares_stale_suppressed) {
      const det = s.suppression_detail || {};
      const reason = reasonText[s.suppression_reason] || s.suppression_reason || 'unknown';
      const stored = det.stored_account || 'unknown';
      const storedMode = det.stored_mode || 'unknown';
      const current = det.current_account || 'unknown';
      const asOf = det.as_of ? new Date(det.as_of).toISOString() : 'unknown';
      const rows = det.row_count == null ? '?' : det.row_count;
      $('suppressedPaperBody').textContent =
        `reason: ${reason}  |  stored: account=${stored} mode=${storedMode}  |  ` +
        `current: account=${current}  |  as_of=${asOf}  |  ${rows} row(s) hidden`;
      suppressedBanner.style.display = 'block';
      staleBanner.style.display = 'none';
    } else if (s.ib_positions_stale) {
      const st = s.ib_positions_stale;
      const stored = st.stored_account || 'unknown';
      const current = st.current_account || 'unknown';
      const asOf = st.as_of ? new Date(st.as_of).toISOString() : 'unknown';
      $('staleSnapshotBody').textContent =
        `Last refresh from account ${stored} at ${asOf}. ` +
        `Awaiting refresh from live account ${current}.`;
      staleBanner.style.display = 'block';
      suppressedBanner.style.display = 'none';
    } else {
      staleBanner.style.display = 'none';
      suppressedBanner.style.display = 'none';
    }

    // T-DASH-STale-POSITIONS-BOTH-DASHBOARDS1: render position-state
    // staleness banner from /state.stale_position_state. Server-side
    // computes per-file status (fresh | stale | missing) plus an
    // overall stale/missing flag; we only render.
    const stalePosBanner = $('stalePositionsBanner');
    const sps = s.stale_position_state || {};
    if (sps.stale || sps.missing) {
      const parts = (sps.files || []).filter(f => f.status !== 'fresh').map(f => {
        const age = f.age_hours == null ? 'missing' : `${f.age_hours}h old`;
        return `${f.label} (${f.path}): ${f.status} — ${age}`;
      });
      $('stalePositionsBody').innerHTML = parts.length
        ? parts.map(p => `<div>• ${p}</div>`).join('')
        : 'all position sidecars stale or missing.';
      stalePosBanner.style.display = 'block';
    } else {
      stalePosBanner.style.display = 'none';
    }

    // T-LIVE-CYCLE-EVIDENCE-BUNDLE-DASHBOARD-LINK1: render the
    // state/live_cycle_evidence.json bundle status card. Read-only:
    // /state.live_cycle_evidence_meta is the source of truth.
    const lce = s.live_cycle_evidence_meta || {};
    const lceBody = $('liveCycleEvidenceBody');
    if (lceBody) {
      if (lce.status === 'fresh') {
        const exDelta = lce.exec_log_count_in_window;
        const decDelta = lce.decisions_count_in_window;
        const ibAge = lce.ib_positions_hours_old;
        lceBody.innerHTML =
          `<span style="color:var(--bull)">fresh</span> ` +
          `(${lce.age_hours ?? '?'}h old) — ` +
          `account ${lce.account_id || 'unknown'} — ` +
          `run ${lce.launcher_ts || 'unknown'}<br>` +
          `<span style="color:var(--muted)">` +
          `exec_log Δ=${exDelta ?? '?'}  decisions Δ=${decDelta ?? '?'}  ` +
          `ib_positions ${ibAge == null ? 'unknown' : ibAge + 'h'} old` +
          `</span>`;
      } else if (lce.status === 'stale') {
        lceBody.innerHTML =
          `<span style="color:var(--warn)">stale</span> ` +
          `(${lce.age_hours ?? '?'}h old) — re-run menu 41 to refresh`;
      } else if (lce.status === 'missing') {
        const advice = lce.advice || 'no live cycle has run yet';
        lceBody.innerHTML =
          `<span style="color:var(--muted)">missing</span> — ${advice}` +
          (lce.arming_mode === 'supervised_one_cycle'
            ? ' <span style="color:var(--warn)">(next: menu 41)</span>'
            : '');
      } else {
        lceBody.innerHTML =
          `<span style="color:var(--bear)">unreadable</span> — sidecar exists but failed to parse`;
      }
    }

    $('equity').textContent = '$' + fmt(c.equity);
    const regime = c.regime || '—';
    $('regime').innerHTML = `<span class="pill p-${cls(regime)}">${regime}</span>`;
    $('positions').textContent = (c.open_positions || 0) + ' / 6';
    $('entriesAllowed').innerHTML = c.entries_allowed
      ? '<span class="bull">yes</span>' : '<span class="warn">no</span>';

    const heatV = c.heat || 0;
    $('heat').textContent = (heatV * 100).toFixed(2) + '%';
    const heatPct = Math.min(heatV / 0.05, 1) * 100;
    const bar = $('heatBar');
    bar.style.width = heatPct + '%';
    bar.style.background = heatV > 0.05 ? 'var(--bear)' : heatV > 0.03 ? 'var(--warn)' : 'var(--bull)';

    const mt = c.market_timing || {};
    $('mtState').innerHTML = `<span class="pill p-${cls(mt.state === 'confirmed_uptrend' ? 'bull' : mt.state === 'correction' ? 'bear' : 'neutral')}">${mt.state || '—'}</span>`;
    $('ddCount').textContent = mt.distribution_days ?? '—';
    $('ftdToday').innerHTML = mt.is_ftd_today ? '<span class="bull">yes</span>' : 'no';
    $('sectorLeaders').textContent = (c.sector_leaders || []).join(' ') || '—';

    const pos = s.positions || {};
    const body = $('posBody');
    // Stock-only — option-shape entries render in the Open Option Positions table.
    // No Sell button: options-v1.2 does not sell stocks from the dashboard
    // (2026-05-08 incident). Use scripts/sell_position.py --emergency for stocks
    // or scripts/sell_option.py for option contracts.
    const syms = Object.keys(pos).filter(k => !('contracts' in pos[k] && 'premium_entry' in pos[k]));
    if (syms.length === 0) {
      body.innerHTML = '<tr><td colspan="8" style="color:var(--muted);text-align:center;padding:20px">no open positions</td></tr>';
    } else {
      body.innerHTML = syms.map(sym => {
        const p = pos[sym];
        const entry = p.entry, peak = p.peak || entry, stop = p.stop;
        const initialStop = p.initial_stop || stop;
        const gain = peak && entry ? (peak - entry) / entry : 0;
        const rMultiple = entry && initialStop && initialStop < entry ? (peak - entry) / (entry - initialStop) : 0;
        const layerName = ['pilot', 'half', 'full'][p.layer] || '?';
        // T-DASH-STale-POSITIONS-BOTH-DASHBOARDS1: positions.json rows
        // carrying manage=false are operator-held. Show that
        // explicitly so they aren't read as bot-tracked.
        const operatorHeld = p.manage === false;
        const heldBadge = operatorHeld
          ? ' <span title="manage=false" style="font-size:10px;color:var(--warn);padding:1px 5px;border:1px solid var(--warn);border-radius:3px;margin-left:4px">operator-held</span>'
          : '';
        return `<tr>
          <td><b>${sym}</b>${heldBadge}</td>
          <td>${layerName}</td>
          <td>${p.shares}</td>
          <td>$${fmt(entry)}</td>
          <td>$${fmt(stop)}</td>
          <td>$${fmt(peak)}</td>
          <td class="${gain > 0 ? 'bull' : gain < 0 ? 'bear' : ''}">${pct(gain)}</td>
          <td class="${rMultiple >= 2 ? 'bull' : ''}">${rMultiple.toFixed(2)}R</td>
        </tr>`;
      }).join('');
    }

    // Bot-tracked option positions — live inside positions.json alongside
    // stocks, discriminated by shape (contracts + premium_entry = option).
    // No Sell button — exits go through run.sh menu (S2 invariant).
    const optEntries = Object.entries(pos).filter(
      ([, p]) => 'contracts' in p && 'premium_entry' in p);
    const optBody = $('optPosBody');
    // T-LAUNCH5 expiry-risk strip: surface DTE-near-zero positions as a
    // read-only operator signal. EXPIRY_RISK_DTE matches the
    // exercise-guard window so the strip warns about the same set of
    // positions the bot will defensively close.
    const EXPIRY_RISK_DTE = 3;
    const strip = $('expiryRiskStrip');
    if (optEntries.length === 0) {
      optBody.innerHTML = '<tr><td colspan="8" style="color:var(--muted);text-align:center;padding:20px">no open option positions</td></tr>';
      strip.style.display = 'none';
    } else {
      const dteList = optEntries.map(([sym, p]) => {
        const dteLeft = (p.dte_days != null && p.entry_date)
          ? p.dte_days - Math.floor((Date.now() - Date.parse(p.entry_date)) / 86400000)
          : p.dte_days;
        return {sym, dte: dteLeft};
      });
      const nearExpiry = dteList
        .filter(r => r.dte != null && r.dte <= EXPIRY_RISK_DTE)
        .sort((a, b) => (a.dte ?? 999) - (b.dte ?? 999));
      const minDte = dteList.reduce((m, r) =>
        (r.dte != null && (m == null || r.dte < m)) ? r.dte : m, null);
      let stripColor, stripTag;
      if (nearExpiry.length > 0) {
        stripColor = 'var(--bear)';
        stripTag = `EXPIRY RISK: ${nearExpiry.length} position(s) within ${EXPIRY_RISK_DTE} DTE`;
      } else {
        stripColor = 'var(--muted)';
        stripTag = `expiry risk: clear (nearest ${minDte ?? '—'} DTE)`;
      }
      const detail = nearExpiry.length > 0
        ? ' — ' + nearExpiry.map(r => `${r.sym}(${r.dte}d)`).join(', ')
        : '';
      strip.innerHTML =
        `<span style="color:${stripColor};font-weight:600">${stripTag}</span>${detail}`;
      strip.style.display = 'block';

      optBody.innerHTML = optEntries.map(([sym, p]) => {
        const right = p.right || 'C';
        const expRaw = p.expiry || '';
        const expFmt = expRaw.length === 8 ? `${expRaw.slice(0,4)}-${expRaw.slice(4,6)}-${expRaw.slice(6,8)}` : expRaw;
        const entry = p.premium_entry;
        const cur = p.current_premium ?? p.peak_option_value ?? entry;
        const gain = cur && entry ? (cur - entry) / entry : 0;
        const dteLeft = (p.dte_days != null && p.entry_date)
          ? p.dte_days - Math.floor((Date.now() - Date.parse(p.entry_date)) / 86400000)
          : p.dte_days;
        return `<tr>
          <td><b>${sym}</b></td>
          <td>${right} $${fmt(p.strike)}</td>
          <td>${expFmt}</td>
          <td>${p.contracts}</td>
          <td>$${fmt(entry)}</td>
          <td>$${fmt(cur)}</td>
          <td class="${gain > 0 ? 'bull' : gain < 0 ? 'bear' : ''}">${pct(gain)}</td>
          <td>${dteLeft ?? '—'}</td>
        </tr>`;
      }).join('');
    }

    // Untracked panels — IB has it but state.json doesn't.
    // Split STK from OPT so option contracts don't render with stock fields
    // (which previously made AAPL options look like "1 share @ $680" and
    // got wrongly claimed → bot stop-exit'd them next cycle).
    try {
      const tracked = new Set(syms);
      const stockRows = [];
      const optionRows = [];
      const ut = $('untrackedBody');
      const utOpt = $('untrackedOptBody');

      if (ibPositionsForUi && Object.keys(ibPositionsForUi).length) {
        Object.entries(ibPositionsForUi).forEach(([key, info]) => {
          // SAFETY: require explicit sec_type. Missing field = stale snapshot
          // from pre-fix code; refuse to render with a Sell button (used to
          // default to STK and that's how the 2026-05-08 naked-short clicks
          // happened — option rows looked like stock rows).
          const secType = info.sec_type;
          const sym = info.symbol || key;
          if (!secType) return;  // skip untagged entries entirely
          if (secType === 'OPT') {
            const exp = info.expiry || '';
            const expFmt = exp.length === 8 ? `${exp.slice(0,4)}-${exp.slice(4,6)}-${exp.slice(6,8)}` : exp;
            optionRows.push(`<tr>
              <td><b>${sym}</b></td>
              <td>$${fmt(info.strike)}${info.right || 'C'}</td>
              <td>${expFmt}</td>
              <td>${info.contracts}</td>
              <td>$${fmt(info.implied_premium)}</td>
              <td>$${fmt(info.avg_cost_per_contract)}</td>
              <td>
                <button class="btn-sell-opt"
                        data-sym="${sym}"
                        data-strike="${info.strike}"
                        data-expiry="${exp}"
                        data-right="${info.right || 'C'}"
                        data-contracts="${info.contracts}">Sell</button>
              </td>
            </tr>`);
          } else if (secType === 'STK') {
            if (!tracked.has(sym)) {
              stockRows.push(`<tr>
                <td><b>${sym}</b></td>
                <td>${info.shares}</td>
                <td>$${fmt(info.avg_cost)}</td>
                <td>
                  <button class="btn-claim" data-sym="${sym}">Claim</button>
                </td>
              </tr>`);
            }
          }
          // any other secType (FUT, BOND, CASH, ...): skip — no UI handler
        });
      }

      // Stocks panel
      if (stockRows.length) {
        ut.innerHTML = stockRows.join('');
        ut.querySelectorAll('.btn-claim').forEach(btn => {
          btn.onclick = async () => {
            const sym = btn.dataset.sym;
            if (!confirm(`Claim ${sym} into bot tracking? Adds to positions.json.`)) return;
            btn.disabled = true;
            const r = await mutate(`/claim?symbol=${sym}&confirm=YES`);
            if (!r) { alert('cancelled — operator token required'); btn.disabled = false; return; }
            const j = await r.json();
            alert(`${sym} claim:\n${j.stdout || j.error}`);
            btn.disabled = false;
          };
        });
      } else {
        ut.innerHTML = sharesSuppressed
          ? '<tr><td colspan="4" style="color:var(--warn);text-align:center;padding:12px">'
            + 'live shares suppressed — stale paper snapshot (see banner above)'
            + '</td></tr>'
          : '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:12px">none — bot state matches IB</td></tr>';
      }

      // Options panel — Sell button fires manual SELL via /sell_option
      if (optionRows.length) {
        utOpt.innerHTML = optionRows.join('');
        utOpt.querySelectorAll('.btn-sell-opt').forEach(btn => {
          btn.onclick = async () => {
            const sym = btn.dataset.sym;
            const strike = btn.dataset.strike;
            const expiry = btn.dataset.expiry;
            const right = btn.dataset.right;
            const contracts = btn.dataset.contracts;
            if (!confirm(`Sell ${contracts}x ${sym} $${strike}${right} ${expiry}?\nPlaces real order against IB.`)) return;
            // Disable EVERY sell-opt button while one is in flight. Prevents
            // the 2026-05-08 duplicate-click path that opened naked shorts
            // when a fill landed between the first click and the UI refresh.
            const allBtns = document.querySelectorAll('.btn-sell-opt');
            allBtns.forEach(b => { b.disabled = true; });
            btn.textContent = 'selling...';
            try {
              const r = await mutate(
                `/sell_option?symbol=${sym}&strike=${strike}&expiry=${expiry}` +
                `&right=${right}&contracts=${contracts}&confirm=YES`);
              if (!r) {
                alert('cancelled — operator token required');
              } else {
                const j = await r.json();
                alert(`${sym} option sell:\n${j.stdout || j.error || JSON.stringify(j)}`);
              }
            } catch (e) {
              alert(`error: ${e.message}`);
            }
            // Force a fresh /state read before any further clicks. The
            // re-rendered table only shows positions IB still confirms.
            btn.textContent = 'Sell';
            await tick();
          };
        });
      } else {
        utOpt.innerHTML = sharesSuppressed
          ? '<tr><td colspan="7" style="color:var(--warn);text-align:center;padding:12px">'
            + 'options suppressed — stale paper snapshot (see banner above)'
            + '</td></tr>'
          : '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:12px">none</td></tr>';
      }
    } catch (e) { /* ignore */ }

    const ev = $('eventsList');
    ev.innerHTML = (e || []).slice().reverse().map(evt => {
      const ts = (evt.ts || '').slice(11, 19);
      const action = evt.event || '';
      const sym = evt.symbol ? `<b>${evt.symbol}</b>` : '';
      const extra = Object.entries(evt).filter(([k]) => !['ts', 'event', 'symbol'].includes(k))
        .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
        .join(' ');
      return `<div class="event ${action}"><span class="ts">${ts}</span> <span class="act">${action}</span> ${sym} ${extra}</div>`;
    }).join('');

    if (q && q.length > 1) {
      drawSpark(q);
      const first = q[0].equity, curr = q[q.length - 1].equity;
      $('eqFirst').textContent = '$' + fmt(first);
      $('eqCurrent').textContent = '$' + fmt(curr);
      const ret = (curr - first) / first;
      $('eqReturn').innerHTML = `<span class="${ret >= 0 ? 'bull' : 'bear'}">${pct(ret)}</span>`;
    }

    // Watchlist as table, sorted by %chg descending
    if (w && w.tickers) {
      const groups = w.groups || {};
      const ffty = new Set(groups.ffty || []);
      const mag7 = new Set(groups.mag7 || []);
      const sp500 = new Set(groups.sp500_top50 || []);
      const fallback = new Set(groups.fallback || []);
      const activeOptions = new Set(groups.active_options || []);
      const held = new Set(Object.keys(s.positions || {}));
      const quotes = (qs && qs.quotes) || {};
      const sz = w.sizes || {};

      $('wlTotal').textContent = `${w.tickers.length} tickers`;
      // T-DASH-WLISTOPS1: prominent count card mirror.
      $('wlCountBig').textContent = w.tickers.length;
      // T-LAUNCH5 watchlist-health: explicit source + age + colored
      // status tag. Operator can see at a glance whether the bot is
      // running on a fresh build, a stale build, or a fallback set.
      const sourceStr = w.source || (w.note || 'config.SYMBOLS');
      const ageDays = (() => {
        if (!w.as_of) return null;
        const t = Date.parse(w.as_of);
        if (isNaN(t)) return null;
        return (Date.now() - t) / 86400000;
      })();
      const isFallback = !!sz.fallback || !!sz.active_options || /fallback|fetch-failed|empty/i.test(sourceStr);
      let tag = 'OK';
      let tagColor = 'var(--bull)';
      if (isFallback) { tag = 'FALLBACK'; tagColor = 'var(--warn)'; }
      else if (ageDays !== null && ageDays > 14) { tag = `STALE ${ageDays.toFixed(0)}d`; tagColor = 'var(--bear)'; }
      else if (ageDays !== null && ageDays > 7) { tag = `STALE ${ageDays.toFixed(0)}d`; tagColor = 'var(--warn)'; }
      const ageStr = ageDays === null ? '' :
        (ageDays < 1 ? ' (<1d)' : ` (${ageDays.toFixed(0)}d)`);
      $('wlSummary').innerHTML = sz.active_options
        ? `<span style="color:${tagColor};font-weight:600">[${tag}]</span> active options ${sz.active_options} · ${w.note || sourceStr}`
        : sz.fallback
        ? `<span style="color:${tagColor};font-weight:600">[${tag}]</span> fallback ${sz.fallback} · ${w.note || sourceStr}`
        : `<span style="color:${tagColor};font-weight:600">[${tag}]</span> ${sourceStr} · as of ${w.as_of || '—'}${ageStr}`;
      // T-DASH-WLISTOPS1: mirror tag + source into the prominent count
      // card's meta line so freshness is readable next to the number.
      $('wlCountMeta').innerHTML =
        `<span style="color:${tagColor};font-weight:600">[${tag}]</span> `
        + `${sourceStr}${ageStr}`;

      // Build rows with quote data, sort by chg_pct desc
      const rows = w.tickers.map(t => {
        const q = quotes[t] || {};
        const memberships = [ffty.has(t), mag7.has(t), sp500.has(t)].filter(Boolean).length;
        let srcCls = 'src';
        if (memberships > 1) srcCls += ' multi';
        else if (activeOptions.has(t)) srcCls += ' sp500';
        else if (fallback.has(t)) srcCls += ' sp500';
        else if (mag7.has(t)) srcCls += ' mag7';
        else if (ffty.has(t)) srcCls += ' ffty';
        else if (sp500.has(t)) srcCls += ' sp500';
        const srcTag = [ffty.has(t) && 'F', mag7.has(t) && 'M', sp500.has(t) && 'S', fallback.has(t) && 'CFG', activeOptions.has(t) && 'OPT'].filter(Boolean).join('');
        return {
          sym: t,
          last: q.last,
          chg_pct: q.chg_pct == null ? -999 : q.chg_pct,
          vol_ratio: q.vol_ratio,
          rs_rank: q.rs_rank,
          held: held.has(t),
          srcCls,
          srcTag,
        };
      });
      rows.sort((a, b) => b.chg_pct - a.chg_pct);

      $('wlBody').innerHTML = rows.map(r => {
        const chgCls = r.chg_pct == null || r.chg_pct === -999 ? '' :
          r.chg_pct >= 3 ? 'big-up' : r.chg_pct >= 0 ? 'up' :
          r.chg_pct <= -3 ? 'big-down' : 'down';
        const chgTxt = r.chg_pct == null || r.chg_pct === -999 ? '—' :
          (r.chg_pct >= 0 ? '+' : '') + r.chg_pct.toFixed(2) + '%';
        const lastTxt = r.last == null ? '—' : r.last.toFixed(2);
        const volTxt = r.vol_ratio == null || r.vol_ratio === 0 ? '—' : r.vol_ratio.toFixed(1) + '×';
        const rsTxt = r.rs_rank == null ? '—' : r.rs_rank;
        return `<tr class="${r.held ? 'held' : ''}">
          <td><b>${r.sym}</b></td>
          <td class="num">${lastTxt}</td>
          <td class="num ${chgCls}">${chgTxt}</td>
          <td class="num">${volTxt}</td>
          <td class="num">${rsTxt}</td>
          <td><span class="${r.srcCls}">${r.srcTag}</span></td>
        </tr>`;
      }).join('');
    }

    $('lastUpdate').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (err) {
    $('lastUpdate').textContent = 'error: ' + err.message;
  }
}

function drawSpark(points) {
  const cvs = $('spark');
  const ctx = cvs.getContext('2d');
  const w = cvs.width, h = cvs.height;
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) return;
  const vals = points.map(p => p.equity);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  ctx.strokeStyle = '#80cbc4';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = (i / (points.length - 1)) * w;
    const y = h - ((p.equity - min) / range) * (h - 6) - 3;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// T-DASH-V2-READONLY1 — diagnostic cockpit poll. Independent of tick()
// so an /events or /quotes error does not blank the diagnostics panel,
// and a sidecar read failure does not blank the live cycle view.
async function tickDiagnostics() {
  try {
    const [d, t] = await Promise.all([
      fetch('/diagnostics').then(r => r.json()),
      fetch('/timeline?limit=12').then(r => r.json()),
    ]);
    const flags = d.flags || {};
    const counts = d.counts || {};
    const sidecars = d.sidecars || {};
    const pr = sidecars.portfolio_report || {};

    const pill = (label, v) => {
      let cls = 'p-neutral';
      const text = v == null ? '?' : String(v);
      if (text === 'yes' || text === 'CRITICAL') cls = 'p-bear';
      else if (text === 'WARN' || text === 'warning') cls = 'p-neutral';
      else if (text === 'no' || text === 'HEALTHY') cls = 'p-bull';
      return `<span class="pill ${cls}" style="margin-right:6px">${label}: ${text}</span>`;
    };
    const flagParts = [
      pill('launch_blocker', flags.portfolio_launch_blocker
            ?? flags.timeline_launch_blocker),
      pill('operator_action', flags.portfolio_operator_action
            ?? flags.timeline_operator_action),
      pill('cache_stale', flags.portfolio_cache_stale),
      pill('silent_degradation', flags.portfolio_silent_degradation
            ?? flags.timeline_silent_degradation),
    ];
    if (flags.anomaly_severity) flagParts.push(pill('anomaly', flags.anomaly_severity));
    $('diagFlags').innerHTML = flagParts.join('');

    const ageStr = Object.entries(sidecars).map(([name, sc]) => {
      if (!sc.present) return `${name}: missing`;
      const age = sc.age_minutes == null ? '?' : `${sc.age_minutes}m`;
      const stale = sc.stale ? ' STALE' : '';
      return `${name}: ${age}${stale}`;
    }).join('  ·  ');
    $('diagSidecarAges').textContent = ageStr;

    let ibCount = 0, trackedCount = 0;
    if (pr.present && pr.data && pr.data.by_bot) {
      Object.values(pr.data.by_bot).forEach(b => {
        ibCount += (b.ib_positions || []).length;
        trackedCount += (b.tracked || []).length;
      });
    }
    $('diagIb').textContent = ibCount;
    $('diagTracked').textContent = trackedCount;
    $('diagUnmanaged').textContent = counts.unmanaged ?? '—';
    $('diagOrphan').textContent = counts.orphan ?? '—';
    $('diagExpiry').textContent = counts.expiry_risk ?? '—';

    const diffBody = $('diagDiffBody');
    if (pr.present && pr.data) {
      const rows = [];
      (pr.data.unmanaged || []).forEach(r => {
        rows.push(`<tr><td>${r.bot || '—'}</td><td><b>${r.symbol}</b></td>` +
          `<td>${r.sec_type || '—'}</td><td>${r.qty ?? '—'}</td>` +
          `<td><span class="warn">Unmanaged at broker</span></td></tr>`);
      });
      (pr.data.orphan || []).forEach(r => {
        rows.push(`<tr><td>${r.bot || '—'}</td><td><b>${r.symbol}</b></td>` +
          `<td>${r.sec_type || '—'}</td><td>${r.qty ?? '—'}</td>` +
          `<td><span class="bear">Tracked but absent at broker</span></td></tr>`);
      });
      diffBody.innerHTML = rows.length
        ? rows.join('')
        : '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:8px">no divergence</td></tr>';
    } else {
      diffBody.innerHTML =
        '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:8px">no sidecar generated yet</td></tr>';
    }

    const expBody = $('diagExpiryBody');
    if (pr.present && pr.data && (pr.data.expiry_risk || []).length) {
      expBody.innerHTML = (pr.data.expiry_risk || []).map(r => {
        const sr = `$${r.strike ?? '?'}${r.right ?? ''}`;
        return `<tr><td><b>${r.symbol}</b></td><td>${r.expiry ?? '?'}</td>` +
          `<td>${r.dte ?? '?'}</td><td>${sr}</td>` +
          `<td><span class="warn">${r.risk || 'review'}</span></td></tr>`;
      }).join('');
    } else if (!pr.present) {
      expBody.innerHTML =
        '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:8px">no sidecar generated yet</td></tr>';
    } else {
      expBody.innerHTML =
        '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:8px">none</td></tr>';
    }

    const tlBody = $('diagTimeline');
    if (t.present && t.data && (t.data.events || []).length) {
      const events = (t.data.events || []).slice().reverse();
      tlBody.innerHTML = events.map(e => {
        const ts = (e.ts || '').slice(11, 19);
        const sym = e.symbol ? `<b>${e.symbol}</b>` : '';
        const reason = e.reason ? ` — ${e.reason}` : '';
        return `<div class="event"><span class="ts">${ts}</span> ` +
          `<span class="act">${e.event || '?'}</span> ${sym}${reason}</div>`;
      }).join('');
    } else if (!t.present) {
      tlBody.innerHTML =
        '<div class="event" style="color:var(--muted)">no timeline sidecar generated yet — run scripts/timeline.py --json</div>';
    } else {
      tlBody.innerHTML =
        '<div class="event" style="color:var(--muted)">no recent events</div>';
    }

    const ns = d.notification_schema || {};
    $('diagNotif').innerHTML = ns.installed
      ? `notification formatter installed — ${ns.count} message types: ${(ns.event_types || []).join(', ')}`
      : `notification formatter NOT installed: ${ns.error || 'unknown'}`;
  } catch (err) {
    /* diagnostics is best-effort; do not blank existing UI on error */
  }
}

// T-CHARTS1 — read-only visibility helpers. Pure DOM + canvas, no new
// dependencies. Each renderer tolerates empty / missing input and
// renders an "unavailable" label rather than crashing.
function _chartCtx(id) {
  const c = document.getElementById(id);
  if (!c) return null;
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  if (rect.width && rect.height) {
    c.width = Math.round(rect.width * dpr);
    c.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  ctx.clearRect(0, 0, rect.width || c.width, rect.height || c.height);
  return {ctx, w: rect.width || c.width, h: rect.height || c.height};
}

function drawLineChart(canvasId, points, opts) {
  opts = opts || {};
  const got = _chartCtx(canvasId);
  if (!got) return;
  const {ctx, w, h} = got;
  if (!points || points.length < 2) {
    ctx.fillStyle = '#566';
    ctx.font = '11px ui-monospace,monospace';
    ctx.fillText('no data', 8, 14);
    return;
  }
  const vals = points.map(p => p.y);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  ctx.strokeStyle = opts.color || '#80cbc4';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = (i / (points.length - 1)) * w;
    const y = h - ((p.y - min) / range) * (h - 6) - 3;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  if (opts.fillBelowZero) {
    ctx.fillStyle = 'rgba(239,83,80,0.25)';
    points.forEach((p, i) => {
      if (p.y < 0) {
        const x = (i / (points.length - 1)) * w;
        ctx.fillRect(x - 1, h / 2, 2, h / 2);
      }
    });
  }
}

function drawBarChart(canvasId, bars) {
  const got = _chartCtx(canvasId);
  if (!got) return;
  const {ctx, w, h} = got;
  if (!bars || bars.length === 0) {
    ctx.fillStyle = '#566';
    ctx.font = '11px ui-monospace,monospace';
    ctx.fillText('no data', 8, 14);
    return;
  }
  const vals = bars.map(b => b.value);
  const maxAbs = Math.max(...vals.map(v => Math.abs(v))) || 1;
  const bw = Math.max(1, Math.floor(w / bars.length) - 1);
  const mid = h / 2;
  bars.forEach((b, i) => {
    const x = i * (w / bars.length);
    const hVal = (Math.abs(b.value) / maxAbs) * (h / 2 - 3);
    ctx.fillStyle = b.value >= 0 ? '#66bb6a' : '#ef5350';
    if (b.value >= 0) {
      ctx.fillRect(x, mid - hVal, bw, hVal);
    } else {
      ctx.fillRect(x, mid, bw, hVal);
    }
  });
  ctx.strokeStyle = '#1e2a33';
  ctx.lineWidth = 0.5;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(w, mid);
  ctx.stroke();
}

function renderExposure(containerId, pr) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!pr || !pr.present || !pr.data || !pr.data.by_bot) {
    el.innerHTML = '<span style="color:var(--muted)">no portfolio_report sidecar</span>';
    return;
  }
  let stk = 0, opt = 0;
  Object.values(pr.data.by_bot).forEach(b => {
    (b.ib_positions || []).forEach(r => {
      if (r.sec_type === 'OPT') opt++;
      else stk++;
    });
  });
  const total = stk + opt;
  if (total === 0) {
    el.innerHTML = '<span style="color:var(--muted)">no IB positions in cache</span>';
    return;
  }
  const stkPct = Math.round(100 * stk / total);
  const optPct = 100 - stkPct;
  el.innerHTML = `
    <div style="display:flex;gap:2px;height:18px;width:100%;border-radius:3px;overflow:hidden">
      <div style="background:#66bb6a;width:${stkPct}%" title="STK: ${stk}"></div>
      <div style="background:#80deea;width:${optPct}%" title="OPT: ${opt}"></div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:6px">
      STK: <b style="color:var(--bull)">${stk}</b>
       &nbsp; OPT: <b style="color:#80deea">${opt}</b>
       &nbsp; total: ${total}
    </div>`;
}

function renderDteLadder(bodyId, pr) {
  const el = document.getElementById(bodyId);
  if (!el) return;
  if (!pr || !pr.present || !pr.data || !pr.data.by_bot) {
    el.innerHTML = '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:6px">no portfolio_report sidecar</td></tr>';
    return;
  }
  const seen = new Set();
  const rows = [];
  function parseDte(exp) {
    if (!exp) return null;
    let y, m, d;
    if (exp.length === 8) { y=+exp.slice(0,4); m=+exp.slice(4,6); d=+exp.slice(6,8); }
    else if (exp.length === 10) { y=+exp.slice(0,4); m=+exp.slice(5,7); d=+exp.slice(8,10); }
    else return null;
    const today = new Date(); today.setHours(0,0,0,0);
    const dt = new Date(y, m-1, d); dt.setHours(0,0,0,0);
    return Math.round((dt - today) / 86400000);
  }
  Object.values(pr.data.by_bot).forEach(b => {
    ['ib_positions', 'tracked'].forEach(field => {
      (b[field] || []).forEach(r => {
        if (r.sec_type !== 'OPT') return;
        const key = `${r.symbol}|${r.expiry||''}|${r.strike||''}|${r.right||''}`;
        if (seen.has(key)) return;
        seen.add(key);
        rows.push({sym:r.symbol, expiry:r.expiry, dte:parseDte(r.expiry), qty:r.qty});
      });
    });
  });
  if (rows.length === 0) {
    el.innerHTML = '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:6px">no option positions</td></tr>';
    return;
  }
  rows.sort((a, b) => (a.dte == null ? 9999 : a.dte) - (b.dte == null ? 9999 : b.dte));
  el.innerHTML = rows.slice(0, 12).map(r => {
    const dteStr = r.dte == null ? '?' : r.dte;
    const cls = (r.dte != null && r.dte <= 7) ? 'bear'
              : (r.dte != null && r.dte <= 14) ? 'warn' : '';
    return `<tr>
      <td><b>${r.sym}</b></td>
      <td>${r.expiry || '?'}</td>
      <td class="${cls}">${dteStr}</td>
      <td>${r.qty || '?'}</td>
    </tr>`;
  }).join('');
}

function renderTimelineGroups(containerId, tl) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!tl || !tl.present || !tl.data) {
    el.innerHTML = '<span style="color:var(--muted)">no timeline sidecar — run scripts/timeline.py --json</span>';
    return;
  }
  const events = tl.data.events || [];
  if (events.length === 0) {
    el.innerHTML = '<span style="color:var(--muted)">no recent events</span>';
    return;
  }
  const buckets = {
    'submitted': [], 'filled': [], 'refused': [], 'cancelled': [],
    'data_feed': [], 'watchlist': [], 'gateway': [], 'phantom': [], 'other': []
  };
  events.forEach(e => {
    const ev = (e.event || '').toLowerCase();
    if (ev.includes('submitted')) buckets.submitted.push(e);
    else if (ev.includes('filled')) buckets.filled.push(e);
    else if (ev.includes('refused')) buckets.refused.push(e);
    else if (ev.includes('cancelled')) buckets.cancelled.push(e);
    else if (ev.includes('data_feed')) buckets.data_feed.push(e);
    else if (ev.includes('watchlist')) buckets.watchlist.push(e);
    else if (ev.includes('gateway')) buckets.gateway.push(e);
    else if (ev.includes('phantom')) buckets.phantom.push(e);
    else buckets.other.push(e);
  });
  const order = ['submitted','filled','refused','cancelled','data_feed','watchlist','gateway','phantom','other'];
  const palette = {submitted:'#80cbc4', filled:'#66bb6a', refused:'#ef5350',
                   cancelled:'#ffa726', data_feed:'#ef5350',
                   watchlist:'#ffa726', gateway:'#ef5350', phantom:'#80deea',
                   other:'#7a8a85'};
  el.innerHTML = order
    .filter(k => buckets[k].length > 0)
    .map(k => {
      const n = buckets[k].length;
      return `<div style="display:flex;align-items:center;gap:6px;margin:2px 0">
        <span style="display:inline-block;width:8px;height:8px;background:${palette[k]};border-radius:2px"></span>
        <span style="color:${palette[k]};min-width:90px">${k}</span>
        <span style="color:var(--fg)">${n}</span>
      </div>`;
    }).join('');
}

function _equityPointsFromSeries(series) {
  if (!Array.isArray(series)) return [];
  return series.map(p => ({y: p.equity || p.value || 0, date: p.date}));
}

function _drawdownPointsFromEquity(points) {
  if (!points || points.length < 2) return [];
  let peak = points[0].y;
  return points.map(p => {
    peak = Math.max(peak, p.y);
    const dd = peak > 0 ? (p.y - peak) / peak * 100 : 0;
    return {y: dd, date: p.date};
  });
}

function _dailyPnlBarsFromEquity(points) {
  if (!points || points.length < 2) return [];
  const bars = [];
  for (let i = 1; i < points.length; i++) {
    bars.push({value: points[i].y - points[i-1].y, date: points[i].date});
  }
  return bars;
}

async function tickCharts() {
  try {
    const [eq, pr, tl] = await Promise.all([
      fetch('/equity').then(r => r.json()).catch(() => []),
      fetch('/portfolio_report').then(r => r.json()).catch(() => ({present:false})),
      fetch('/timeline?limit=200').then(r => r.json()).catch(() => ({present:false})),
    ]);
    const eqPoints = _equityPointsFromSeries(eq);
    drawLineChart('chartEquity', eqPoints);
    if (eqPoints.length) {
      const last = eqPoints[eqPoints.length-1].y;
      const first = eqPoints[0].y;
      const ret = first ? ((last - first) / first * 100).toFixed(1) : '?';
      $('chartEquityMeta').innerHTML =
        `latest $${last.toLocaleString(undefined,{maximumFractionDigits:0})} ` +
        `&nbsp;|&nbsp; period return ${ret}%`;
    } else {
      $('chartEquityMeta').textContent = 'no equity_history yet';
    }

    const ddPoints = _drawdownPointsFromEquity(eqPoints);
    drawLineChart('chartDrawdown', ddPoints, {color: '#ef5350', fillBelowZero: true});
    if (ddPoints.length) {
      const minDd = Math.min(...ddPoints.map(p => p.y));
      $('chartDrawdownMeta').innerHTML =
        `max drawdown ${minDd.toFixed(1)}%`;
    } else {
      $('chartDrawdownMeta').textContent = 'no equity_history yet';
    }

    const bars = _dailyPnlBarsFromEquity(eqPoints).slice(-60);
    drawBarChart('chartPnl', bars);
    if (bars.length) {
      const last = bars[bars.length-1].value;
      $('chartPnlMeta').innerHTML =
        `last day ${last >= 0 ? '+' : ''}$${last.toLocaleString(undefined,{maximumFractionDigits:0})}` +
        ` &nbsp;|&nbsp; ${bars.length} bars`;
    } else {
      $('chartPnlMeta').textContent = 'need at least 2 equity points';
    }

    renderExposure('chartExposure', pr);
    renderDteLadder('chartDteLadderBody', pr);
    renderTimelineGroups('chartTimelineGroups', tl);
  } catch (err) {
    /* charts are best-effort; failure here must not blank tick() */
  }
}

// T-DASH-WLISTOPS1 — Rebuild Watchlist button wiring (one-shot).
const rebuildBtn = document.getElementById('btn-rebuild-watchlist');
if (rebuildBtn) {
  rebuildBtn.onclick = async () => {
    const msg = 'Rebuild the watchlist now?\n\n'
              + 'This runs scripts/build_watchlist.py, which:\n'
              + '  - re-fetches FFTY holdings (or uses the fallback\n'
              + '    universe + degraded-overwrite guard if FFTY is\n'
              + '    unreachable)\n'
              + '  - refreshes versions/<bot>/state/watchlist.json\n\n'
              + 'NO ORDERS are placed. Existing trading state is\n'
              + 'untouched. May take up to 60s.';
    if (!confirm(msg)) return;
    rebuildBtn.disabled = true;
    const original = rebuildBtn.textContent;
    rebuildBtn.textContent = 'rebuilding…';
    try {
      const r = await mutate('/rebuild_watchlist?confirm=YES');
      if (!r) { alert('cancelled — operator token required'); return; }
      const j = await r.json();
      const ok = j.status === 'completed';
      const head = ok ? 'rebuild OK' : 'rebuild FAILED';
      const body = j.stdout || j.stderr || j.error || JSON.stringify(j);
      alert(`watchlist ${head}:\n${body}`);
      try { tick(); } catch (e) { /* tick is best-effort here */ }
    } catch (e) {
      alert(`error: ${e}`);
    } finally {
      rebuildBtn.disabled = false;
      rebuildBtn.textContent = original;
    }
  };
}

tick();
tickDiagnostics();
tickCharts();
setInterval(tick, 5000);
setInterval(tickDiagnostics, 5000);
setInterval(tickCharts, 5000);
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML.replace("__COMMIT__", COMMIT)
