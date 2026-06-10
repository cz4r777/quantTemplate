"""Trading bot orchestrator — enforces data/playbook/rules.md.

Cycle outline (runs per invocation, not continuous):
  1. Gate 1 — market regime (HMM on SPY) must permit entries
  2. Load existing positions; update stops, check exits, consider pyramid adds
  3. For the rest of the watchlist (candidates): apply Gate 2 (Trend Template)
     and Gate 3 (valid breakout). Open pilot positions while within safety
     limits.
  4. Persist state, notify, write dashboard state.json.
"""

from __future__ import annotations

# Safety guard intentionally runs before project/broker imports.
# ruff: noqa: E402
import json
import logging
import os
from pathlib import Path
from typing import Any


def _enforce_live_launcher_cycle_gate() -> None:
    """Live main.py cycles must come from tools/live_launcher.py only.

    T-PYTEST-LIVE-GUARD-BYPASS1 — IMPORT-TIME bypass for pytest. Pytest
    sets PYTEST_CURRENT_TEST in os.environ per-test; a normal operator
    shell does NOT set it. The bypass keys solely on that sentinel,
    so it cannot be triggered from a normal shell (no LIVE_TEST_BYPASS
    or similar shell-settable knob exists by design). It is IMPORT-
    TIME only — the broker / order / launcher paths downstream still
    require their own confirmations, so a test cannot place an order
    via this bypass.
    """
    mode = os.getenv("IBKR_MODE", "paper").strip().lower() or "paper"
    if mode != "live":
        return
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    account_id = os.getenv("LIVE_ACCOUNT_ID", "").strip()
    confirm = os.getenv("LIVE_MODE_CONFIRM", "").strip()
    one_cycle = os.getenv("LIVE_LAUNCHER_ONE_CYCLE", "").strip()
    expected = f"ENABLE LIVE TRADING ON ACCOUNT {account_id}" if account_id else ""
    if not (one_cycle == "1" and account_id and confirm == expected):
        raise RuntimeError(
            "live main.py requires the launcher cycle marker. Reach this "
            "path only via tools/live_launcher.py. Refusing."
        )


_enforce_live_launcher_cycle_gate()

import pandas as pd
from allocation.option_sizer import option_contracts, risk_multiplier_for_macro_regime
from brain.gate import evaluate as evaluate_gate
from brain.index_gate import index_uptrend_gate, is_index
from brain.options import option_selector as _option_selector_mod
from brain.options.option_selector import select_contract
from brain.regime import classify_with_hysteresis as deterministic_regime
from execution.option_manager import open_position as open_option_position
from safety.phantom_guard import option_qty_at_broker as _option_qty_at_broker

from brain.breakout import detect as detect_breakout
from brain.data_feed import fetch_ohlcv
from brain.earnings_filter import blackout as earnings_blackout
from brain.fundamentals import can_slim_check
from brain.hmm_classifier import RegimeClassifier
from brain.market_timing import assess as assess_market_timing
from brain.rs_rank import compute_ranks
from brain.sector_strength import is_in_leading_sector, leading_sectors
from brain.stage_engine import trend_template
from broker.ibkr_client import IBKRClient
from config import (
    APPLY_FUNDAMENTALS,
    HMM_STATES,
    LOOKBACK_DAYS,
    MAG7,
    MARKET_GATE_MODE,
    MAX_POSITIONS,
    REGIME_ALLOWED_FOR_ENTRY,
    STATE_FILE,
    SYMBOLS,
    WATCHLIST_FILE,
)
from execution import position_manager as pm
from execution.event_log import log as log_event
from execution.safe_io import single_instance
from notifications.smsbot import send, send_message
from safety.circuit_breaker import CircuitBreaker
from safety.portfolio_heat import compute_heat

# Module-level cache derived from a config import. Kept BELOW imports so
# ruff E402 doesn't fire on the brain/* imports that follow MAG7. (Was
# wedged between import blocks in earlier revisions — T-MAIN-IMPORT-ORDER1.)
MAG7_SET = set(MAG7)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
WATCHLIST_STALE_DAYS = 7
PHANTOM_DELETE_AFTER = 3
EXERCISE_GUARD_DTE = 1


def load_symbols() -> list[str]:
    p = Path(WATCHLIST_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            tickers = data.get("tickers") or []
            if tickers:
                return tickers
        except (json.JSONDecodeError, KeyError) as e:
            log_event("watchlist_unreadable_in_load_symbols", error=f"{type(e).__name__}: {e}")
    return SYMBOLS


def watchlist_warning() -> str | None:
    p = Path(WATCHLIST_FILE)
    fallback = ",".join(SYMBOLS)
    if not p.exists():
        return f"missing; fallback={fallback}"
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return f"unreadable:{e}; fallback={fallback}"

    tickers = data.get("tickers") or []
    if not tickers:
        return f"empty; fallback={fallback}"

    as_of = data.get("as_of")
    if not as_of:
        return "missing_as_of"
    ts = pd.to_datetime(as_of, errors="coerce", utc=True)
    if pd.isna(ts):
        return f"bad_as_of:{as_of}"
    age_days = (pd.Timestamp.now("UTC").date() - ts.date()).days
    if age_days > WATCHLIST_STALE_DAYS:
        return f"stale:{age_days}d as_of={as_of}"
    return None


def market_regime(bench_df: pd.DataFrame) -> str:
    """Classify market regime.

    Default: deterministic SMA-stack classifier (brain/regime.py) with
    hysteresis. Set USE_HMM_REGIME=true in .env to revert to legacy
    HMM (brain/hmm_classifier.py) — kept as a one-flag rollback.
    """
    use_hmm = os.environ.get("USE_HMM_REGIME", "").lower() in ("1", "true", "yes")

    if use_hmm:
        try:
            clf = RegimeClassifier(n_states=HMM_STATES)
            clf.fit(bench_df)
            return clf.predict(bench_df)
        except Exception as e:
            log.warning("HMM regime classification failed: %s — defaulting to 'neutral'", e)
            log_event("hmm_fallback", reason=str(e))
            return "neutral"

    try:
        return deterministic_regime(bench_df, Path("state/regime.json"))
    except Exception as e:
        log.warning("Deterministic regime failed: %s — defaulting to 'neutral'", e)
        log_event("regime_fallback", reason=str(e))
        return "neutral"


def _is_option_position(pos: dict) -> bool:
    """Option positions track 'contracts' + 'premium_entry'; stock positions
    track 'shares' + 'stop'. The shape disambiguates which manager to use."""
    return "contracts" in pos and "premium_entry" in pos


def _option_key(sym: str, expiry: str, strike: float, right: str = "C") -> tuple:
    return (sym, "OPT", round(float(strike or 0), 4), expiry or "", right or "C")


def _tracked_option_keys(positions: dict) -> set[tuple]:
    keys = set()
    for sym, pos in positions.items():
        if _is_option_position(pos):
            keys.add(
                _option_key(
                    sym,
                    pos.get("expiry", ""),
                    pos.get("strike", 0),
                    pos.get("right", "C"),
                )
            )
    return keys


def _days_to_expiry(expiry: str) -> int | None:
    ts = pd.to_datetime(expiry, format="%Y%m%d", errors="coerce")
    if pd.isna(ts):
        ts = pd.to_datetime(expiry, errors="coerce")
    if pd.isna(ts):
        return None
    today = pd.Timestamp.now(tz="America/New_York").date()
    return (ts.date() - today).days


def _has_open_option_sell_order(
    broker: IBKRClient, sym: str, expiry: str, strike: float, right: str
) -> bool:
    try:
        trades = broker.ib.openTrades()
    except Exception as e:
        log_event(
            "exercise_guard_open_order_check_failed",
            symbol=sym,
            strike=strike,
            expiry=expiry,
            right=right,
            reason=str(e),
        )
        return True

    terminal = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
    for trade in trades:
        contract = getattr(trade, "contract", None)
        order = getattr(trade, "order", None)
        status = getattr(getattr(trade, "orderStatus", None), "status", "")
        if not contract or not order or status in terminal:
            continue
        if getattr(order, "action", "") != "SELL":
            continue
        if (
            getattr(contract, "secType", "") == "OPT"
            and getattr(contract, "symbol", "") == sym
            and getattr(contract, "lastTradeDateOrContractMonth", "") == expiry
            and abs(float(getattr(contract, "strike", 0) or 0) - float(strike)) < 0.01
            and getattr(contract, "right", "") == right
        ):
            return True
    return False


def _close_untracked_expiring_options(broker: IBKRClient, positions: dict) -> list[dict]:
    """Close untracked long options near expiry to avoid surprise exercise.

    This only acts on broker-held OPT positions that are absent from
    positions.json. Tracked options still flow through option_manager's exit
    rules, including the existing DTE close.
    """
    tracked = _tracked_option_keys(positions)
    actions: list[dict] = []
    try:
        ib_positions = list(broker.ib.positions())
    except Exception as e:
        log_event("exercise_guard_scan_failed", reason=str(e))
        return actions

    for ib_pos in ib_positions:
        contract = ib_pos.contract
        if getattr(contract, "secType", "") != "OPT":
            continue
        qty = float(ib_pos.position)
        if qty <= 0:
            continue

        sym = contract.symbol
        expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
        strike = float(getattr(contract, "strike", 0) or 0)
        right = getattr(contract, "right", "C")
        dte = _days_to_expiry(expiry)
        if dte is None or dte < 0 or dte > EXERCISE_GUARD_DTE:
            continue
        if _option_key(sym, expiry, strike, right) in tracked:
            continue
        if _has_open_option_sell_order(broker, sym, expiry, strike, right):
            log_event(
                "exercise_guard_order_exists",
                symbol=sym,
                strike=strike,
                expiry=expiry,
                right=right,
                dte=dte,
            )
            continue

        contracts = int(qty)
        if contracts <= 0:
            continue
        # T-NOTIFY-WIRE1 — surface the risk BEFORE the auto-close attempt so
        # operator sees the detection independent of whether the close
        # succeeded.
        from notifications import smsbot as _smsbot
        from notifications.trade_messages import exercise_risk

        _smsbot.send_message(
            exercise_risk(
                symbol=sym,
                expiry=expiry,
                dte=dte,
                strike=strike,
                right=right,
                qty=contracts,
            )
        )
        try:
            rec = broker.place_option_order(
                symbol=sym,
                expiry=expiry,
                strike=strike,
                right=right,
                action="SELL",
                contracts=contracts,
                order_type="MKT",
                wait_secs=10.0,
            )
        except Exception as e:
            log_event(
                "exercise_guard_close_failed",
                symbol=sym,
                strike=strike,
                expiry=expiry,
                right=right,
                dte=dte,
                reason=str(e),
            )
            send(
                f"EXERCISE GUARD FAILED {sym} {contracts}x ${strike:g}{right} {expiry}: {e}",
                "warnings",
            )
            continue

        log_event(
            "exercise_guard_close",
            symbol=sym,
            strike=strike,
            expiry=expiry,
            right=right,
            contracts=contracts,
            dte=dte,
            ib_status=rec.get("ib_status"),
            status=rec.get("status"),
        )
        send(
            f"EXERCISE GUARD SELL {sym} {contracts}x ${strike:g}{right} "
            f"{expiry} ({dte} DTE, untracked)",
            "trades",
        )
        actions.append(
            {
                "symbol": sym,
                "action": "exercise_guard_close",
                "contracts": contracts,
                "strike": strike,
                "expiry": expiry,
                "right": right,
                "dte": dte,
                "trade": rec,
            }
        )
    return actions


def _manage_option_position(
    sym: str,
    pos: dict,
    df: pd.DataFrame,
    broker: IBKRClient,
) -> dict | None:
    """Run option-specific exit logic. Returns the action dict if exit/trim
    fired, else None. Closes via broker.place_option_order(SELL)."""
    from execution import option_manager as om

    # CRITICAL: phantom-position guard. If positions.json has a record but
    # IB doesn't hold the contract, refuse to act. Placing a SELL on
    # contracts we don't own would open a naked short.
    expiry = pos.get("expiry") or ""
    if not expiry:
        log_event("phantom_position_skipped", symbol=sym, reason="missing_expiry_in_state")
        return None
    right = pos.get("right", "C")
    broker_qty = _option_qty_at_broker(broker, sym, pos["strike"], expiry, right)
    if broker_qty is None:
        log_event(
            "phantom_position_skipped",
            symbol=sym,
            strike=pos["strike"],
            expiry=expiry,
            reason="broker_query_failed",
        )
        return None
    if broker_qty <= 0:
        miss_count = int(pos.get("phantom_not_held_count", 0)) + 1
        pos["phantom_not_held_count"] = miss_count
        log_event(
            "phantom_position_skipped",
            symbol=sym,
            strike=pos["strike"],
            expiry=expiry,
            reason="not_held_at_broker",
            miss_count=miss_count,
        )
        if miss_count >= PHANTOM_DELETE_AFTER:
            log_event(
                "phantom_position_removed",
                symbol=sym,
                strike=pos["strike"],
                expiry=expiry,
                reason="confirmed_not_held_at_broker",
                miss_count=miss_count,
            )
            send(
                f"REMOVED PHANTOM {sym} ${pos['strike']}C {expiry}: "
                f"IB confirmed not held {miss_count}x",
                "warnings",
            )
            return {
                "symbol": sym,
                "action": "remove_phantom_option",
                "reason": "confirmed_not_held_at_broker",
                "contracts": pos.get("contracts", 0),
                "_full_close": True,
            }
        return None
    pos.pop("phantom_not_held_count", None)

    today = pd.Timestamp.now("UTC").date().isoformat()
    signal = om.evaluate_exit(pos, df, today)
    if signal is None:
        return None

    n_close = int(signal.close_contracts)
    if n_close <= 0:
        return None

    try:
        rec = broker.place_option_order(
            symbol=sym,
            expiry=expiry,
            strike=pos["strike"],
            right=right,
            action="SELL",
            contracts=n_close,
            order_type="MID",
        )
    except Exception as e:
        log_event("option_close_failed", symbol=sym, reason=str(e))
        return None

    # IB-side cancellation (e.g., contract delisted) — don't mutate state
    if rec.get("status") == "cancelled":
        log_event(
            "option_close_cancelled",
            symbol=sym,
            reason=rec.get("error", "unknown"),
            ib_status=rec.get("ib_status"),
            strike=pos["strike"],
            expiry=expiry,
        )
        return None

    if signal.action == "close":
        om.close(sym, {sym: pos})  # remove from local copy
        # also remove from caller's positions dict (handled by caller)
        log_event(
            "option_close",
            symbol=sym,
            reason=signal.reason,
            contracts=n_close,
            strike=pos["strike"],
            expiry=expiry,
        )
        send(f"CLOSE {sym} {n_close}x ${pos['strike']}C {expiry} ({signal.reason})", "trades")
        return {
            "symbol": sym,
            "action": "close_option",
            "reason": signal.reason,
            "contracts": n_close,
            "trade": rec,
            "_full_close": True,
        }
    else:  # trim
        pos["contracts"] = pos["contracts"] - n_close
        pos["partial_2x_taken"] = True
        log_event(
            "option_trim",
            symbol=sym,
            reason=signal.reason,
            trimmed=n_close,
            remaining=pos["contracts"],
        )
        send(f"TRIM {sym} -{n_close}x ${pos['strike']}C ({signal.reason})", "trades")
        return {
            "symbol": sym,
            "action": "trim_option",
            "reason": signal.reason,
            "contracts": n_close,
            "trade": rec,
        }


def _open_long_call_entry(
    sym: str,
    df: pd.DataFrame,
    equity: float,
    broker: IBKRClient,
    positions: dict,
    risk_multiplier: float,
    entry_signal: str,
    breakout=None,
) -> dict | None:
    """Open a long call for a qualified index or stock-underlying signal."""
    today = pd.Timestamp.now("UTC").date().isoformat()
    try:
        contract = select_contract(df, sym, today)
    except Exception as e:
        log_event("skip", symbol=sym, reason=f"select_contract_failed:{e}")
        return None
    if contract is None:
        log_event("skip", symbol=sym, reason="no_contract_available")
        return None

    # Currency guard (T-BOT-LIVE-CCY-GUARD-FIX1) — bot trades US options
    # (SMART/USD). Allow if the account actually holds USD cash OR if
    # the operator has explicitly added USD to
    # ACCEPTED_CONTRACT_CURRENCIES (auto-FX / margin override).
    from allocation.position_sizer import currency_mismatch_reason
    from config import accepted_contract_currencies

    ccy_reason = currency_mismatch_reason(
        broker.account_cash_currencies(),
        accepted_contract_currencies(),
        "USD",
    )
    if ccy_reason:
        log_event("skip", symbol=sym, reason=ccy_reason)
        try:
            from notifications.trade_messages import trade_refused

            send_message(
                trade_refused(
                    sym,
                    "BUY",
                    ccy_reason,
                    expiry=contract.expiry,
                    strike=contract.strike,
                    right="C",
                )
            )
        except Exception as e:
            log_event("notify_dispatch_failed", error=f"{type(e).__name__}: {e}")
        return None

    n_contracts = option_contracts(equity, contract.premium, risk_multiplier)
    if n_contracts <= 0:
        log_event("skip", symbol=sym, reason=f"sizing_zero:premium={contract.premium}")
        return None

    try:
        rec = broker.place_option_order(
            symbol=sym,
            expiry=contract.expiry,
            strike=contract.strike,
            right="C",
            action="BUY",
            contracts=n_contracts,
            order_type="MID",
        )
    except Exception as e:
        log_event("entry_failed", symbol=sym, error=str(e), entry_signal=entry_signal)
        return None

    # IB-side cancellation (Error 200, etc) - DO NOT record state.
    if rec.get("status") == "cancelled":
        log_event(
            "entry_cancelled",
            symbol=sym,
            reason=rec.get("error", "unknown"),
            ib_status=rec.get("ib_status"),
            strike=contract.strike,
            expiry=contract.expiry,
            entry_signal=entry_signal,
        )
        return None

    # Strike-grid / contract-validation refusal (T-P0-STRIKEGRID1).
    # Broker returned invalid_contract because IB has no security
    # definition for this strike+expiry. Do NOT write tracked state.
    if rec.get("status") == "invalid_contract":
        log_event(
            "entry_invalid_contract",
            symbol=sym,
            strike=contract.strike,
            expiry=contract.expiry,
            right="C",
            reason=rec.get("error", "no security definition"),
            ib_status=rec.get("ib_status"),
            entry_signal=entry_signal,
        )
        return None

    # Post-submit re-query (T-P0-STRIKEGRID1). The broker's wait window
    # may be too short for IB to deliver a late async cancel (e.g. the
    # "Error 200 lands after 3s" pattern). Re-query the live IB position
    # for THIS contract before writing tracked state. If IB has 0
    # contracts at this exact identity, refuse the write — the order
    # either has not filled yet (will be picked up on a later cycle once
    # IB confirms) or was async-cancelled. Either way: no phantom row.
    try:
        held_now = broker._option_position_qty(sym, contract.expiry, contract.strike, "C")
    except Exception as e:
        log_event(
            "entry_requery_failed",
            symbol=sym,
            strike=contract.strike,
            expiry=contract.expiry,
            right="C",
            reason=f"{type(e).__name__}: {e}",
            entry_signal=entry_signal,
        )
        return None
    if held_now <= 0:
        log_event(
            "entry_unconfirmed_at_broker",
            symbol=sym,
            strike=contract.strike,
            expiry=contract.expiry,
            right="C",
            ib_status=rec.get("ib_status"),
            held_now=held_now,
            reason="post-submit re-query shows 0 contracts held; refusing to write tracked state",
            entry_signal=entry_signal,
        )
        return None

    contract.entry_date = today
    open_option_position(contract, n_contracts, positions)

    action = {
        "symbol": sym,
        "action": "open_option",
        "contracts": n_contracts,
        "strike": contract.strike,
        "expiry": contract.expiry,
        "premium": contract.premium,
        "entry_signal": entry_signal,
        "trade": rec,
    }
    if breakout is not None:
        action["breakout"] = breakout.is_breakout
        action["pocket_pivot"] = breakout.is_pocket_pivot

    log_event(
        "open",
        symbol=sym,
        contracts=n_contracts,
        strike=contract.strike,
        expiry=contract.expiry,
        premium=contract.premium,
        dte=contract.dte_days,
        entry_signal=entry_signal,
        breakout=getattr(breakout, "is_breakout", None),
        pocket_pivot=getattr(breakout, "is_pocket_pivot", None),
    )
    send(
        f"OPEN {sym} {n_contracts}x ${contract.strike}C {contract.expiry} "
        f"@ ~${contract.premium:.2f} ({entry_signal})",
        "trades",
    )
    return action


def manage_existing(
    positions: dict,
    dfs: dict[str, pd.DataFrame],
    broker: IBKRClient,
) -> list[dict]:
    """Update stops, exit on stop hit or multi-rule signals, pyramid on progress."""
    actions: list[dict[str, Any]] = []
    # Re-check kill switch — user could have touched KILL during the cycle
    if CircuitBreaker().kill_switch_active():
        log_event("kill_switch_detected_mid_cycle")
        return actions
    for sym in list(positions.keys()):
        pos = positions[sym]

        # No-manage flag (T-BOT-LIVE-ENABLEMENT1): operator-seeded
        # positions carry manage=False so the bot will not exit, trim,
        # exercise, or pyramid them. Dashboard still surfaces unrealized
        # P&L for visibility. Default True (existing rows untouched).
        if pos.get("manage", True) is False:
            log_event("skip_unmanaged", symbol=sym, reason="position seeded manage=False")
            continue

        df = dfs.get(sym)
        if df is None or len(df) < 50:
            continue

        # ---- OPTION POSITION PATH ----
        if _is_option_position(pos):
            opt_action = _manage_option_position(sym, pos, df, broker)
            if opt_action is not None:
                if opt_action.pop("_full_close", False):
                    positions.pop(sym, None)
                actions.append(opt_action)
            continue

        # ---- STOCK POSITION PATH (refused in options-v1.2) ----
        # options-v1.2 must not exit, trim, or pyramid stock positions during
        # the normal cycle. The 2026-05-08 incident proved that broker.rebalance
        # in this bot acts on global IB STK positions and is unsafe outside
        # operator-initiated emergency tooling. Stock unwinds belong to
        # scripts/sell_position.py --emergency only.
        log_event(
            "stock_position_skipped",
            symbol=sym,
            reason="options-v1.2 normal cycle is option-only",
            shares=pos.get("shares"),
        )
        actions.append(
            {
                "symbol": sym,
                "action": "skip",
                "reason": "stock_position_in_options_bot",
            }
        )
    return actions


def consider_new_entries(
    candidates: list[str],
    dfs: dict[str, pd.DataFrame],
    rs_ranks: dict[str, float],
    positions: dict,
    equity: float,
    broker: IBKRClient,
    sector_leaders: list[str] | None = None,
    market_timing_state: str = "confirmed_uptrend",
    risk_multiplier: float = 1.0,
) -> list[dict]:
    actions: list[dict[str, Any]] = []
    open_names = set(positions.keys())

    for sym in candidates:
        if sym in open_names:
            continue
        if len(positions) >= MAX_POSITIONS:
            log_event("skip", symbol=sym, reason="max_positions")
            break
        df = dfs.get(sym)
        if df is None or len(df) < 200:
            continue

        # ---- INDEX-OPTIONS PATH ----
        # SPY/QQQ/IWM/DIA: trend_template's per-stock filters (RS rank vs
        # benchmark, multi-MA stack) don't apply. Use the IBD market-timing
        # state + index's own uptrend stack instead, then buy a long call.
        if is_index(sym):
            ok, reason = index_uptrend_gate(sym, df, market_timing_state)
            if not ok:
                log_event("skip", symbol=sym, reason=f"index_gate:{reason}")
                continue

            action = _open_long_call_entry(
                sym, df, equity, broker, positions, risk_multiplier, "index_uptrend"
            )
            if action is not None:
                actions.append(action)
            continue

        # ---- STOCK-UNDERLYING OPTIONS PATH ----
        # options-v1.2 should capture stock breakouts with long calls only.
        # Never route stock candidates through broker.rebalance/STK orders.
        tt = trend_template(df, rs_rank=rs_ranks.get(sym), symbol=sym)
        if not tt.passes:
            failed = [k for k, v in tt.gates.items() if not v]
            log_event("skip", symbol=sym, reason="trend_template", failed_gates=failed)
            continue

        is_mag7 = sym in MAG7_SET
        if sector_leaders and not is_mag7:
            in_sector, sector_reason = is_in_leading_sector(sym, sector_leaders)
            if not in_sector:
                log_event("skip", symbol=sym, reason=sector_reason)
                continue

        bo = detect_breakout(df, symbol=sym, relaxed_volume=is_mag7)
        if not (bo.is_breakout or bo.is_pocket_pivot):
            log_event("skip", symbol=sym, reason="no_breakout", base_count=bo.base_count)
            continue

        if APPLY_FUNDAMENTALS:
            try:
                cs = can_slim_check(sym)
                explicit_fails = [k for k, v in cs.gates.items() if v is False]
                if explicit_fails:
                    log_event(
                        "skip",
                        symbol=sym,
                        reason="canslim_fail",
                        failed=explicit_fails,
                        eps_q=cs.eps_growth_q_yoy,
                        eps_y=cs.eps_growth_annual,
                        roe=cs.roe,
                        inst_pct=cs.inst_pct,
                    )
                    continue
            except Exception as e:
                log_event("canslim_check_error", symbol=sym, error=f"{type(e).__name__}: {e}")

        blocked, ereason = earnings_blackout(sym, strict=False)
        if blocked:
            actions.append({"symbol": sym, "action": "skip", "reason": ereason})
            log_event("skip", symbol=sym, reason=ereason)
            continue

        entry_signal = "stock_breakout" if bo.is_breakout else "pocket_pivot"
        action = _open_long_call_entry(
            sym, df, equity, broker, positions, risk_multiplier, entry_signal, bo
        )
        if action is not None:
            actions.append(action)
    return actions


def _install_option_chain_provider(broker) -> bool:
    """Wire broker.list_option_strikes as the default chain provider
    for select_contract (T-OPT-STRIKEGRID2-WIRE1).

    Returns True when a provider is installed, False when the broker
    doesn't expose list_option_strikes (e.g. a stubbed test broker).
    On the False path, select_contract falls back to the static grid —
    no regression vs. pre-wiring behavior, and the downstream
    T-P0-STRIKEGRID1 guard still blocks invalid contracts.
    """
    fn = getattr(broker, "list_option_strikes", None)
    if not callable(fn):
        _option_selector_mod.set_chain_provider(None)
        return False
    _option_selector_mod.set_chain_provider(fn)
    return True


def run_cycle() -> dict:
    cb = CircuitBreaker()
    broker = IBKRClient()
    broker.connect()
    _install_option_chain_provider(broker)
    # Auto-log every broker.rebalance() call into state/exec_log.jsonl
    from execution import exec_log

    exec_log.wrap_broker(broker)
    exec_log.log(action="cycle_start", symbol="-", notes="options-v1.2 main cycle starting")
    # Snapshot IB positions for dashboard divergence detection
    try:
        from pathlib import Path

        from execution.safe_io import atomic_write_json

        ib_pos_snapshot = {}
        for p in broker.ib.positions():
            qty = int(round(float(p.position)))
            if qty == 0:
                continue
            c = p.contract
            sec_type = getattr(c, "secType", "STK")
            sym = c.symbol
            if sec_type == "OPT":
                expiry = getattr(c, "lastTradeDateOrContractMonth", "")
                strike = float(getattr(c, "strike", 0) or 0)
                right = getattr(c, "right", "C")
                key = f"{sym}_{expiry}_{strike:.2f}{right}"
                ib_pos_snapshot[key] = {
                    "symbol": sym,
                    "sec_type": "OPT",
                    "contracts": qty,
                    "avg_cost_per_contract": float(p.avgCost or 0),
                    "implied_premium": round(float(p.avgCost or 0) / 100.0, 2),
                    "strike": strike,
                    "expiry": expiry,
                    "right": right,
                }
            else:
                ib_pos_snapshot[sym] = {
                    "symbol": sym,
                    "sec_type": "STK",
                    "shares": qty,
                    "avg_cost": float(p.avgCost or 0),
                }
        # T-DASH-SHARES-LIVE-ACCOUNT1: wrap the snapshot in an envelope
        # carrying the account the data came from + when it was taken.
        # Dashboards compare account_id against the current LIVE_ACCOUNT_ID
        # and render a stale-snapshot banner on mismatch.
        import datetime as _dt

        snapshot_account_id: str | None = None
        try:
            managed = broker.ib.managedAccounts() or []
            if managed:
                snapshot_account_id = str(managed[0]).strip() or None
        except Exception:
            snapshot_account_id = None
        if not snapshot_account_id:
            snapshot_account_id = os.getenv("LIVE_ACCOUNT_ID", "").strip() or None
        as_of = _dt.datetime.now(_dt.UTC).isoformat()
        envelope = {
            "schema_version": 2,
            "account_id": snapshot_account_id,
            "as_of": as_of,
            "ibkr_mode": (os.getenv("IBKR_MODE", "").strip().lower() or None),
            "positions": ib_pos_snapshot,
        }
        atomic_write_json(Path("state/ib_positions.json"), envelope)
        # T-ACCOUNT-SUMMARY-SIDECAR1: snapshot account_summary in the same
        # cycle so /account stops returning the placeholder note. Pure
        # read-only IB query; no order side effects. Failure is logged but
        # does NOT abort the cycle — the ib_positions write is the
        # operationally critical one and must always finish.
        try:
            acct_summary = broker.account_summary()
            atomic_write_json(
                Path("state/account_summary.json"),
                {
                    "schema_version": 1,
                    "account_id": snapshot_account_id,
                    "as_of": as_of,
                    "ibkr_mode": envelope["ibkr_mode"],
                    "summary": acct_summary,
                },
            )
        except Exception as _acct_err:
            log_event(
                "account_summary_write_failed",
                error=f"{type(_acct_err).__name__}: {_acct_err}",
            )
        # Append to rolling history — one JSONL line per cycle, used to
        # reconstruct "when did position X disappear" forensically.
        hist_path = Path("state/ib_positions_history.jsonl")
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        with hist_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": as_of,
                        "account_id": snapshot_account_id,
                        "positions": ib_pos_snapshot,
                    },
                    default=str,
                )
                + "\n"
            )
    except Exception as e:
        log_event("ib_history_write_failed", error=f"{type(e).__name__}: {e}")
    try:
        equity = broker.equity()
        ok, reason = cb.check(equity)
        if not ok:
            send(f"Halted: {reason}", "warnings")
            return {"halted": True, "reason": reason, "equity": equity}

        # Gate 1a: HMM regime (primary)
        bench_df = fetch_ohlcv("SPY", LOOKBACK_DAYS)
        if len(bench_df) < 200:
            reason = f"benchmark_data_unavailable: SPY bars={len(bench_df)}"
            state = {"halted": True, "reason": reason, "equity": equity}
            log_event("data_feed_failure", symbol="SPY", reason=reason)
            send(f"Cycle halted: {reason}", "warnings")
            atomic_write_json(STATE_FILE, state, indent=2, default=str)
            return state
        regime = market_regime(bench_df)

        # Gate 1b: IBD market timing (secondary — distribution days + FTD)
        mt = assess_market_timing(bench_df)

        gate_decision = evaluate_gate(
            bench_df,
            hmm_regime=regime,
            mode=MARKET_GATE_MODE,
            regime_allowed_for_entry=REGIME_ALLOWED_FOR_ENTRY,
        )
        entries_allowed = gate_decision.allow_entries

        # Gate 2 context: leading sectors (top-4 SPDR sector ETFs by 3m RS)
        sector_leaders = leading_sectors() if entries_allowed else []

        # Macro regime co-signal — drives per-trade risk multiplier
        # (refreshed weekly by scripts/refresh_macro_regime.py via cron).
        macro_regime = None
        try:
            mr_path = Path("state/macro_regime.json")
            if mr_path.exists():
                macro_regime = json.loads(mr_path.read_text()).get("regime")
        except Exception as e:
            log_event("macro_regime_load_failed", error=f"{type(e).__name__}: {e}")
        risk_mult = risk_multiplier_for_macro_regime(macro_regime)

        log_event(
            "cycle_start",
            regime=regime,
            market_timing=mt.state,
            entries_allowed=entries_allowed,
            sector_leaders=sector_leaders,
            macro_regime=macro_regime,
            risk_multiplier=risk_mult,
            gate_reason=gate_decision.reason,
            gate_path=gate_decision.path,
            sanity_violation=gate_decision.sanity_violation,
        )

        # Load watchlist plus tracked positions. Existing positions must keep
        # getting exit data even if the watchlist source drops them.
        positions = pm.load()
        watchlist_symbols = load_symbols()
        symbols = sorted(set(watchlist_symbols) | set(positions.keys()))
        wl_warning = watchlist_warning()
        if wl_warning:
            log_event("watchlist_warning", reason=wl_warning, symbols=len(symbols))
            send(f"WATCHLIST WARNING: {wl_warning}; scanning {len(symbols)} symbols", "warnings")
        dfs: dict[str, pd.DataFrame] = {}
        fetch_failures: list[tuple[str, str]] = []
        for s in symbols:
            try:
                df = fetch_ohlcv(s, LOOKBACK_DAYS)
                if len(df) >= 50:
                    dfs[s] = df
            except Exception as e:
                fetch_failures.append((s, type(e).__name__))
        if fetch_failures:
            log_event(
                "ohlcv_fetch_errors",
                count=len(fetch_failures),
                sample=fetch_failures[:5],
            )
        # Feed-health gate (T-P0-DATA1).  Block NEW ENTRIES when the data
        # feed coverage is too thin to trust gate decisions.  manage_existing
        # is left running so stops/exits/phantom-cleanup still operate on the
        # subset of symbols that DID return data (each per-symbol path
        # already null-checks its df before acting).  Healthy-feed behavior
        # is unchanged.
        feed_blocks_entries = False
        if not dfs:
            log_event("data_feed_failure", reason="no_ohlcv_loaded", symbols=len(symbols))
            from notifications import smsbot as _smsbot
            from notifications.trade_messages import data_feed_unhealthy

            _smsbot.send_message(
                data_feed_unhealthy(
                    provider="yfinance",
                    ratio=0.0,
                    missing=len(symbols),
                    detail="no entries can be evaluated",
                )
            )
        elif len(dfs) < max(5, len(symbols) // 4):
            log_event("data_feed_degraded", loaded=len(dfs), symbols=len(symbols))
            from notifications import smsbot as _smsbot
            from notifications.trade_messages import data_feed_unhealthy

            _smsbot.send_message(
                data_feed_unhealthy(
                    provider="yfinance",
                    ratio=round(len(dfs) / max(1, len(symbols)), 3),
                    missing=len(symbols) - len(dfs),
                )
            )
            feed_blocks_entries = True
        if not dfs:
            feed_blocks_entries = True
        if feed_blocks_entries:
            log_event(
                "data_feed_unhealthy",
                loaded=len(dfs),
                symbols=len(symbols),
                fetch_failures=len(fetch_failures),
                reason="entries_blocked: insufficient OHLCV coverage",
            )

        rs_ranks = compute_ranks(dfs)

        # Reconcile our tracked positions vs the broker's actual positions.
        # Log divergences — don't auto-fix (too risky without human review).
        try:
            recon = broker.reconcile(positions)
            if recon["divergences"] or recon["orphans"] or recon["untracked"]:
                log_event("position_reconciliation", **recon)
                import datetime as _dt

                rev_path = Path("state/reconcile_events.jsonl")
                rev_path.parent.mkdir(parents=True, exist_ok=True)
                ts = _dt.datetime.utcnow().isoformat()
                with rev_path.open("a", encoding="utf-8") as f:
                    for o in recon["orphans"]:
                        f.write(
                            json.dumps(
                                {
                                    "ts": ts,
                                    "event": "orphan",
                                    "symbol": o["symbol"],
                                    "sec_type": o.get("sec_type", "STK"),
                                    "strike": o.get("strike", 0),
                                    "expiry": o.get("expiry", ""),
                                    "right": o.get("right", ""),
                                    "tracked": o["tracked"],
                                    "actual": 0,
                                }
                            )
                            + "\n"
                        )
                    for d in recon["divergences"]:
                        f.write(
                            json.dumps(
                                {
                                    "ts": ts,
                                    "event": "divergence",
                                    "symbol": d["symbol"],
                                    "sec_type": d.get("sec_type", "STK"),
                                    "strike": d.get("strike", 0),
                                    "expiry": d.get("expiry", ""),
                                    "right": d.get("right", ""),
                                    "tracked": d["tracked"],
                                    "actual": d["actual"],
                                }
                            )
                            + "\n"
                        )
                    for u in recon["untracked"]:
                        f.write(
                            json.dumps(
                                {
                                    "ts": ts,
                                    "event": "untracked",
                                    "symbol": u["symbol"],
                                    "sec_type": u.get("sec_type", "STK"),
                                    "strike": u.get("strike", 0),
                                    "expiry": u.get("expiry", ""),
                                    "right": u.get("right", ""),
                                    "actual": u["actual"],
                                }
                            )
                            + "\n"
                        )
                for o in recon["orphans"]:
                    log.warning(
                        "orphan position in state: %s tracked=%d actual=0 — manually remove or check broker",
                        o["symbol"],
                        o["tracked"],
                    )
                for d in recon["divergences"]:
                    log.warning(
                        "position divergence: %s tracked=%d actual=%d",
                        d["symbol"],
                        d["tracked"],
                        d["actual"],
                    )
        except Exception as e:
            log.warning("reconciliation check failed: %s", e)

        # Snapshot per-ticker quotes for the dashboard watchlist
        quotes: dict = {}
        for sym, df in dfs.items():
            if len(df) < 2:
                continue
            last = float(df["Close"].iloc[-1])
            prev = float(df["Close"].iloc[-2])
            chg_pct = ((last / prev) - 1.0) if prev > 0 else 0.0
            vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
            vol_avg = float(df["Volume"].tail(50).mean()) if "Volume" in df.columns else 0.0
            quotes[sym] = {
                "last": round(last, 2),
                "prev": round(prev, 2),
                "chg_pct": round(chg_pct * 100, 2),
                "vol_ratio": round(vol / vol_avg, 2) if vol_avg > 0 else 0.0,
                "rs_rank": round(rs_ranks.get(sym, 0), 0),
            }
        atomic_write_json(
            "state/ticker_quotes.json",
            {
                "as_of": pd.Timestamp.now("UTC").isoformat(),
                "quotes": quotes,
            },
            indent=2,
            default=str,
        )

        # Stage A: manage existing positions always (even in bear regime)
        managed = _close_untracked_expiring_options(broker, positions)
        managed += manage_existing(positions, dfs, broker)

        # Stage B: new entries only if regime permits AND feed is healthy.
        # The feed_blocks_entries gate is the explicit data-quality
        # control-plane stop (T-P0-DATA1).
        new_entries = []
        if entries_allowed and not feed_blocks_entries:
            new_entries = consider_new_entries(
                candidates=list(dfs.keys()),
                dfs=dfs,
                rs_ranks=rs_ranks,
                positions=positions,
                equity=equity,
                broker=broker,
                sector_leaders=sector_leaders,
                market_timing_state=mt.state,
                risk_multiplier=risk_mult,
            )
        elif entries_allowed and feed_blocks_entries:
            log_event(
                "entries_blocked_data_feed_unhealthy",
                loaded=len(dfs),
                symbols=len(symbols),
            )

        pm.save(positions)

        state = {
            "equity": equity,
            "regime": regime,
            "market_timing": mt.to_dict(),
            "gate_inputs": gate_decision.summary_dict(),
            "gate_reason": gate_decision.reason,
            "entries_allowed": entries_allowed,
            "open_positions": len(positions),
            "heat": compute_heat(positions, equity),
            "managed": managed,
            "new_entries": new_entries,
            "positions": positions,
        }
        atomic_write_json(STATE_FILE, state, indent=2, default=str)

        # Append to equity history (JSONL, for dashboard sparkline)
        Path("state/equity_history.jsonl").parent.mkdir(parents=True, exist_ok=True)
        with Path("state/equity_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": pd.Timestamp.now("UTC").isoformat(),
                        "equity": equity,
                        "regime": regime,
                        "open_positions": len(positions),
                        "heat": state["heat"],
                    },
                    default=str,
                )
                + "\n"
            )

        return state
    finally:
        broker.disconnect()


if __name__ == "__main__":
    try:
        # Prevent concurrent cron fires from stepping on each other
        with single_instance("state/main.pid"):
            state = run_cycle()
            print(
                f"regime={state.get('regime')} "
                f"positions={state.get('open_positions')} "
                f"heat={state.get('heat', 0):.2%}"
            )
    except RuntimeError as e:
        # Another instance running — log and exit quietly (don't alarm)
        log.warning("%s", e)
    except Exception as e:
        log.error("cycle failed: %s", e)
        send(f"Cycle failed: {e}", "warnings")
        raise
