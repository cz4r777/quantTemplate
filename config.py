import os

from dotenv import load_dotenv

if not os.getenv("PYTEST_CURRENT_TEST"):
    load_dotenv()

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))  # 4002 = IB Gateway paper
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "6"))  # 6 = options-v1.2
# (2=v1.2, 4=options-v1.1, 5=stress, 6=options-v1.2)

# --- Paper/live config guard (T-LAUNCH3 + T-LIVE-CONFIG-SPLIT1) ---------
# Default invariant: paper-only.
#
# Live mode is permitted ONLY when every condition below is satisfied,
# so a misconfigured deployment cannot drift into live by accident.
# The only sanctioned path to satisfy all conditions is to launch via
# tools/live_launcher.py (T-LIVE-LAUNCHER1), which:
#   - requires the operator's identity-bearing confirm phrase
#   - sets IBKR_MODE=live, IBKR_PORT in LIVE_PORTS, LIVE_ACCOUNT_ID,
#     LIVE_LAUNCHER_ONE_CYCLE=1, LIVE_MODE_CONFIRM=<canonical phrase>
#   - runs main.py exactly once, then exits (no cron path)
#
# Ports:
#   4001  IB Gateway live    requires full live env
#   7496  TWS live           requires full live env
#   4002  IB Gateway paper   ALLOWED (default)
#   7497  TWS paper          ALLOWED
# Any other port is refused (unknown intent).
LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}
IBKR_MODE = os.getenv("IBKR_MODE", "paper").strip().lower() or "paper"
_LIVE_ACCOUNT_ID = os.getenv("LIVE_ACCOUNT_ID", "").strip()
_LIVE_MODE_CONFIRM = os.getenv("LIVE_MODE_CONFIRM", "").strip()
_LIVE_ONE_CYCLE = os.getenv("LIVE_LAUNCHER_ONE_CYCLE", "").strip()
_LIVE_CANONICAL_CONFIRM = (
    f"ENABLE LIVE TRADING ON ACCOUNT {_LIVE_ACCOUNT_ID}" if _LIVE_ACCOUNT_ID else ""
)
LIVE_LAUNCHER_CYCLE_MARKER = (
    _LIVE_ONE_CYCLE == "1"
    and bool(_LIVE_ACCOUNT_ID)
    and _LIVE_MODE_CONFIRM == _LIVE_CANONICAL_CONFIRM
)
LIVE_CONSUMER_MARKER = _LIVE_MODE_CONFIRM in {
    "DASHBOARD_RUNTIME_ONLY",
    "RUN_SH_HELPER_ONLY",
}
_LIVE_IMPORT_MARKER = LIVE_LAUNCHER_CYCLE_MARKER or LIVE_CONSUMER_MARKER

if IBKR_MODE not in ("paper", "live"):
    raise RuntimeError(
        f"IBKR_MODE={IBKR_MODE!r} is unknown. "
        "Allowed values: 'paper' (default) or 'live' (reachable only "
        "via tools/live_launcher.py). Refusing to load config."
    )

if IBKR_PORT in LIVE_PORTS:
    # A live port is acceptable only when every live-mode condition is set.
    if IBKR_MODE != "live":
        raise RuntimeError(
            f"live IBKR_PORT={IBKR_PORT} refused: IBKR_MODE={IBKR_MODE!r} "
            "is not 'live'. Live port requires IBKR_MODE=live AND a "
            "launcher marker. Refusing to load config."
        )
    if not _LIVE_ACCOUNT_ID:
        raise RuntimeError(
            f"live IBKR_PORT={IBKR_PORT} refused: LIVE_ACCOUNT_ID env "
            "not set. Live mode must run via tools/live_launcher.py "
            "which sets this. Refusing to load config."
        )
    if not _LIVE_IMPORT_MARKER:
        raise RuntimeError(
            f"live IBKR_PORT={IBKR_PORT} refused: live import marker "
            "missing. Use tools/live_launcher.py for live cycles, or a "
            "read-only consumer marker for dashboard/run.sh helpers. "
            "Refusing to load config."
        )
elif IBKR_MODE == "live":
    # Live mode declared but the port is not a live port — refuse the
    # contradiction rather than silently downgrade.
    raise RuntimeError(
        f"IBKR_MODE=live with non-live IBKR_PORT={IBKR_PORT} refused. "
        f"Set IBKR_PORT in {sorted(LIVE_PORTS)} when running live, "
        "or revert to IBKR_MODE=paper. Refusing to load config."
    )
elif IBKR_PORT not in PAPER_PORTS:
    # Unknown port — refuse rather than guess intent.
    raise RuntimeError(
        f"IBKR_PORT={IBKR_PORT} is not a recognised live or paper port. "
        f"Allowed paper: {sorted(PAPER_PORTS)}. "
        f"Allowed live: {sorted(LIVE_PORTS)} (gated by live-launcher marker). "
        "Refusing to load config."
    )

# --- Phase rollout caps (T-LIVE-PHASE1-CAPS1) -----------------------------
# Live mode is reachable only inside an approved rollout phase. Phase1 has
# the tightest caps and is the only phase the operator should pick for the
# first live boot. Paper mode ignores these caps.
LIVE_ALLOWED_PHASES = {"phase1", "phase2", "phase3"}
LIVE_ROLLOUT_PHASE = os.getenv("LIVE_ROLLOUT_PHASE", "").strip().lower() or None
PHASE1_MAX_POSITIONS = 1
PHASE1_RISK_PER_TRADE_MAX = 0.0125

if IBKR_MODE == "live":
    if LIVE_ROLLOUT_PHASE is None:
        raise RuntimeError(
            "live mode refused: LIVE_ROLLOUT_PHASE env not set. "
            "Live boots must declare a rollout phase (start with "
            "'phase1') via tools/live_launcher.py --phase phase1. "
            "Refusing to load config."
        )
    if LIVE_ROLLOUT_PHASE not in LIVE_ALLOWED_PHASES:
        raise RuntimeError(
            f"live mode refused: LIVE_ROLLOUT_PHASE={LIVE_ROLLOUT_PHASE!r} "
            f"is unknown. Allowed: {sorted(LIVE_ALLOWED_PHASES)}."
        )

SYMBOLS = ["SPY", "QQQ"]  # fallback if watchlist.json is missing
WATCHLIST_FILE = "state/watchlist.json"

# Mag 7 — mega-caps under continuous institutional accumulation.
# Treated as a special group: pass on volume-surge requirement (their baseline
# volume is already enormous) and sector-leadership gate. Still must pass all
# 8 Trend Template gates, base count, and earnings blackout.
MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
LOOKBACK_DAYS = 500
HMM_STATES = 5

# --- Minervini risk model -----------------------------------------------
# Tuned 2026-04 via scripts/tune.py (combo_C): +95% 5yr, 9.9% max DD, Sharpe 1.30
# Defaults are the normal paper-mode values; the live launcher
# (T-LIVE-PHASE1-OVERRIDE1) injects phase1 overrides via env so paper
# config never needs to be edited.
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.025"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "6"))
PILOT_FRACTIONS = [0.33, 0.67, 1.00]  # pilot → 2/3 → full (bigger start)
DEFAULT_STOP_PCT = 0.06  # 6% default (tighter = more shares per $ risk)
MIN_STOP_PCT = 0.02  # tight-stop floor
MAX_STOP_PCT = 0.08  # hard ceiling — no loose stops

# --- Market entry gate --------------------------------------------------
# v1.2: switched from HMM+DD stacked gates to Minervini price-trend. The
# old stack gated us out of 77% of 2021 (when SPY was +27%) because the DD
# counter was in "under pressure" for most of the year. Price-trend is
# transparent, robust, and matches the discretionary Minervini playbook.
#
#   "price" mode: SPY > 21-DMA AND 21-DMA > 50-DMA > 200-DMA AND 21-DMA rising
#   "hmm+dd"    : legacy stack (HMM regime + DD counter < 4)
MARKET_GATE_MODE = "hmm+dd"

# Legacy HMM/DD config — retained for diagnostics + "hmm+dd" mode A/B.
REGIME_ALLOWED_FOR_ENTRY = {"neutral", "bull", "bull_run"}

# --- Fundamentals gate (CAN SLIM) ---------------------------------------
# v1.1 decision: keep FMP infrastructure in place but DO NOT gate entries on
# it. The FFTY/IBD watchlist is already pre-screened for CAN SLIM-quality
# fundamentals by IBD's own filters — adding another hard fundamentals gate
# is redundant and cuts trade count too aggressively (halved round trips,
# missed real winners). Minervini uses CAN SLIM-style fundamentals as input
# to his watchlist; we do the same by SOURCING the watchlist from FFTY.
# Code is preserved so v2.0 can re-enable with point-in-time data.
APPLY_FUNDAMENTALS = False
SLIPPAGE_BPS = 15  # per-side slippage; 15bps is realistic for
# liquid large-caps (FFTY + MAG7 + SP500 top-50)

# --- Safety -------------------------------------------------------------
MAX_DAILY_LOSS_PCT = 0.02  # hard halt for the day
MAX_DRAWDOWN_PCT = 0.05  # kill switch
KILL_SWITCH_FILE = "state/KILL"
STATE_FILE = "state/state.json"

SMSBOT_URL = "http://127.0.0.1:8001/send-message"
DASHBOARD_PORT = 8082  # was 8080 — v1.2 uses 8080, options-v1.1 uses 8081.
# Distinct port so all three dashboards can run concurrently.

# --- Phase1 cap enforcement (T-LIVE-PHASE1-CAPS1) -------------------------
# Now that MAX_POSITIONS and RISK_PER_TRADE are defined above, refuse to
# load if the live-mode operator left them above the phase1 cap.
# Paper mode ignores this — the caps are a live-rollout discipline.
if IBKR_MODE == "live" and LIVE_ROLLOUT_PHASE == "phase1":
    if MAX_POSITIONS > PHASE1_MAX_POSITIONS:
        raise RuntimeError(
            f"phase1 live refused: MAX_POSITIONS={MAX_POSITIONS} "
            f"exceeds phase1 cap of {PHASE1_MAX_POSITIONS}. Tighten "
            "config or pick a later approved phase. Refusing to load."
        )
    if RISK_PER_TRADE > PHASE1_RISK_PER_TRADE_MAX:
        raise RuntimeError(
            f"phase1 live refused: RISK_PER_TRADE={RISK_PER_TRADE} "
            f"exceeds phase1 cap of {PHASE1_RISK_PER_TRADE_MAX}. "
            "Tighten config or pick a later approved phase. Refusing to load."
        )


# ── Currency override (T-BOT-LIVE-CCY-GUARD-FIX1) ─────────────────────
# Operators with multi-currency IB accounts (e.g. AUD base + USD cash)
# can explicitly permit contracts in currencies their accountValues
# lookup might not surface — IBKR's auto-FX / margin treatment lets a
# USD contract trade against AUD without a positive USD cash row.
#
# Env shape:
#   ACCEPTED_CONTRACT_CURRENCIES=USD
#   ACCEPTED_CONTRACT_CURRENCIES=USD,AUD
# Normalized to uppercase; whitespace stripped; empty entries ignored.
# Unset / empty / whitespace-only -> empty set (no override).
def accepted_contract_currencies() -> set[str]:
    raw = os.environ.get("ACCEPTED_CONTRACT_CURRENCIES", "") or ""
    return {s.strip().upper() for s in raw.split(",") if s.strip()}
