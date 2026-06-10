#!/usr/bin/env bash
# tradingbot options-v1.2 — single operator menu
#
# USAGE:  bash run.sh        (interactive menu)
#         bash run.sh <N>    (run menu item N non-interactively)
#
# When new tools are added, extend this file — don't make the operator
# remember another command.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# VSCode sync drops the +x bit. Make our shell scripts executable on every run
# so a fresh `git pull` (or remote-sync) doesn't break the menu silently.
chmod 775 deploy/*.sh scripts/*.sh run.sh 2>/dev/null || true

# Stop git from seeing chmod changes as dirty — without this, `git pull` errors
# out with "Your local changes would be overwritten" after every auto-chmod.
git config core.fileMode false 2>/dev/null || true

# Colors
BOLD=$'\e[1m'
DIM=$'\e[2m'
OK=$'\e[32m'
WARN=$'\e[33m'
FAIL=$'\e[31m'
RESET=$'\e[0m'

activate_venv() {
    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    else
        echo "${FAIL}venv missing — run option 1 (setup) first${RESET}"
        return 1
    fi
}

pause() {
    echo
    read -r -p "Press Enter to return to menu..." _
}

# T-RUN-DASHTOKEN1 — persisted DASHBOARD_MUTATION_TOKEN handling.
# Token lives at $HOME/.config/tradingbot/dashboard_token as raw bytes
# (no shell quoting, no trailing newline). chmod 600. Auto-load on
# every menu start so subsequent actions (menu 39 smoke test, menu 38
# readiness, menu 40 guided live prep) all see the same token without
# the operator having to remember to `export` it. NEVER printed.
DASHBOARD_TOKEN_FILE="$HOME/.config/tradingbot/dashboard_token"

load_persisted_dashboard_token() {
    if [ -z "${DASHBOARD_MUTATION_TOKEN:-}" ] \
       && [ -r "$DASHBOARD_TOKEN_FILE" ] \
       && [ -s "$DASHBOARD_TOKEN_FILE" ]; then
        local tok
        tok="$(cat "$DASHBOARD_TOKEN_FILE" 2>/dev/null || true)"
        if [ -n "$tok" ]; then
            export DASHBOARD_MUTATION_TOKEN="$tok"
        fi
        tok=""
    fi
}
load_persisted_dashboard_token

# T-RUN-LIVEWIZARD-LIVEPORT1: read a single KEY from the bot-local
# .env without `source`. Returns the value on stdout. Strips CR,
# surrounding whitespace, and surrounding quotes. Does NOT print the
# value to the terminal — caller decides whether to display it.
# Lines with spaces in the key name are skipped. Comments / blanks
# are skipped. Last-write-wins on duplicate keys.
read_env_key() {
    local key="$1" envfile="${2:-.env}"
    [ -f "$envfile" ] || { printf ''; return; }
    local v
    v="$(grep -E "^${key}=" "$envfile" 2>/dev/null | tail -1 | cut -d= -f2-)"
    v="${v%$'\r'}"
    # strip leading/trailing whitespace
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    # strip surrounding single or double quotes
    case "$v" in
        \"*\") v="${v%\"}"; v="${v#\"}" ;;
        \'*\') v="${v%\'}"; v="${v#\'}" ;;
    esac
    printf '%s' "$v"
}

is_live_env_mode() {
    local mode="${IBKR_MODE:-}"
    [ -z "$mode" ] && mode="$(read_env_key IBKR_MODE)"
    [ "$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')" = "live" ]
}

install_live_helper_markers() {
    # T-RUNSH-LIVE-MARKER-CASCADE1 — allow diagnostic menu helpers to
    # import config.py when .env is live, without opening a live-cycle path.
    # Critical: NEVER set LIVE_LAUNCHER_ONE_CYCLE here. Only
    # tools/live_launcher.py owns that marker.
    is_live_env_mode || return 0

    export IBKR_MODE="live"

    local port acct ccy
    port="${IBKR_PORT:-}"
    [ -z "$port" ] && port="$(read_env_key IBKR_PORT)"
    [ -n "$port" ] && export IBKR_PORT="$port"

    acct="${LIVE_ACCOUNT_ID:-}"
    [ -z "$acct" ] && acct="${IBKR_ACCOUNT_ID:-}"
    [ -z "$acct" ] && acct="$(read_env_key IBKR_ACCOUNT_ID)"
    [ -n "$acct" ] && export LIVE_ACCOUNT_ID="$acct"
    [ -n "$acct" ] && [ -z "${IBKR_ACCOUNT_ID:-}" ] && export IBKR_ACCOUNT_ID="$acct"

    ccy="${ACCEPTED_CONTRACT_CURRENCIES:-}"
    [ -z "$ccy" ] && ccy="$(read_env_key ACCEPTED_CONTRACT_CURRENCIES)"
    [ -n "$ccy" ] && export ACCEPTED_CONTRACT_CURRENCIES="$ccy"

    export LIVE_MODE_CONFIRM="RUN_SH_HELPER_ONLY"
    export LIVE_ROLLOUT_PHASE="phase1"
    export MAX_POSITIONS="1"
    export RISK_PER_TRADE="0.0125"
}
install_live_helper_markers

START_BOT_TARGET="options"
START_BOT_LABEL="options-v1.2"
OTHER_BOT_TARGET="v12"
OTHER_BOT_LABEL="v1.2 swing"

configured_ibkr_port() {
    local port="${IBKR_PORT:-}"
    [ -z "$port" ] && port="$(read_env_key IBKR_PORT)"
    printf '%s' "${port:-4002}"
}

broker_ports_list() {
    ss -tlnp 2>/dev/null | grep -oE ':(4001|4002|7496|7497)\b' | sort -u | tr '\n' ' '
}

port_listening() {
    local port="$1"
    ss -tlnp 2>/dev/null | grep -qE ":${port}\b"
}

discover_tws_bin() {
    ls -1d "$HOME"/Jts/[0-9][0-9][0-9][0-9]/tws1 2>/dev/null | tail -1
}

launch_tws_runtime() {
    local tws_bin
    local tws_ports=""
    tws_ports="$(ss -tlnp 2>/dev/null | grep -oE ':(7496|7497)\b' | sort -u | tr '\n' ' ')"
    if [ -n "$tws_ports" ]; then
        echo "  ${OK}✓${RESET} TWS is already running on${tws_ports}"
        return 0
    fi

    tws_bin="$(discover_tws_bin)"
    if [ -n "$tws_bin" ] && [ -x "$tws_bin" ]; then
        mkdir -p state
        nohup "$tws_bin" > state/tws.log 2>&1 &
        echo "  launched $tws_bin"
        echo "  tail logs: tail -f state/tws.log"
        echo "  Login screen will prompt — pick paper or live"
        sleep 3
        return 0
    fi

    echo "  ${FAIL}✗${RESET} TWS not found. Expected $HOME/Jts/<version>/tws1"
    echo "     See install4j standalone TWS install."
    return 1
}

launch_gateway_runtime() {
    local launcher="${IBGATEWAY_LAUNCHER:-$HOME/ibgateway/ibgateway1}"
    local gw_ports=""
    gw_ports="$(ss -tlnp 2>/dev/null | grep -oE ':(4001|4002)\b' | sort -u | tr '\n' ' ')"
    if [ -n "$gw_ports" ]; then
        echo "  ${OK}✓${RESET} Gateway is already running on${gw_ports}"
        return 0
    fi

    if [ -x "$launcher" ]; then
        mkdir -p state
        nohup "$launcher" > state/ibgateway.log 2>&1 &
        echo "  launched $launcher"
        echo "  tail logs: tail -f state/ibgateway.log"
        sleep 3
        return 0
    fi

    echo "  ${FAIL}✗${RESET} launcher not found: $launcher"
    echo "     Set IBGATEWAY_LAUNCHER env var or check the path."
    return 1
}

kill_gateway_runtime() {
    local gw_ports=""
    gw_ports="$(ss -tlnp 2>/dev/null | grep -oE ':(4001|4002)\b' | sort -u | tr '\n' ' ')"
    if [ -z "$gw_ports" ]; then
        return 0
    fi

    echo "  stale Gateway session detected on${gw_ports}"
    echo "  stopping Gateway/IBC wrapper before switching runtimes..."
    pkill -f "java.*ibgateway" 2>/dev/null || true
    pkill -f "displaybanner" 2>/dev/null || true
    pkill -f "ibcstart" 2>/dev/null || true
    sleep 3

    if ss -tlnp 2>/dev/null | grep -qE ':(4001|4002)\b'; then
        echo "  ${WARN}!${RESET} Gateway port still listening after stop attempt"
        return 1
    fi

    echo "  ${OK}✓${RESET} stale Gateway session cleared"
    return 0
}

refresh_selected_bot_runtime() {
    local start_rc=0

    echo "  stopping ${OTHER_BOT_LABEL} so only the selected bot stays active..."
    bash scripts/bot_control.sh stop "$OTHER_BOT_TARGET" 2>&1 | sed 's/^/    /'
    echo "  refreshing ${START_BOT_LABEL}..."
    bash scripts/bot_control.sh stop "$START_BOT_TARGET" 2>&1 | sed 's/^/    /'
    bash scripts/bot_control.sh start "$START_BOT_TARGET" 2>&1 | sed 's/^/    /'
    start_rc=${PIPESTATUS[0]}
    return "$start_rc"
}

# ---- Menu items --------------------------------------------------------

item_setup() {
    echo "${BOLD}=== First-time / refresh setup ===${RESET}"
    bash deploy/setup.sh
    pause
}

item_start() {
    # T-START-SOURCE-OF-TRUTH1: canonical logged start entrypoint. Every
    # bot/terminal start should flow through here. The function keeps
    # the existing visible operator output AND writes a stable JSON
    # artifact at state/start.log.json (latest run) plus an append-only
    # history line in state/start.log.jsonl. Diagnostics treats that
    # JSON as the source of truth for any future "why did the bot fail
    # to start" investigation; off-path starts (raw `python3 main.py`,
    # cron-only) bypass this file and are lower-trust evidence.
    echo "${BOLD}=== START: broker session + bot ===${RESET}"
    echo

    # T-MENU2-LIVE-START-ORCHESTRATOR-VISIBLE-TODO1 — print the LIVE
    # START TODO board BEFORE any other diagnostic output so the
    # operator sees "what do I run now?" from local evidence without
    # needing to remember 45/48/41/28. Pure read; no mutation.
    local _menu2_bot
    _menu2_bot="$(basename "$(pwd)")"
    case "$_menu2_bot" in
        v1.2|options-v1.2) ;;
        *) _menu2_bot="options-v1.2" ;;
    esac
    if [ -f ../../scripts/menu2_todo_board.py ]; then
        python3 ../../scripts/menu2_todo_board.py \
            --bot "$_menu2_bot" \
            --probe-host "${IBKR_HOST:-127.0.0.1}" \
            --probe-port "$(configured_ibkr_port)" \
            print 2>/dev/null || true
        echo
    fi
    # T-MENU2-ONE-COMMAND-LIVE-START-ORCHESTRATOR1 — if the operator is
    # still disarmed AND wants to enable live trading, offer to open
    # menu 48 from inside menu 2 so they never have to remember the
    # arming menu's number. Menu 48 retains its own phrase gate; this
    # hook just chains the menu items. Selecting [1] continues the
    # normal runtime-only start path. Selecting [2] hands off to menu
    # 48 and falls through here on return so the board re-evaluates
    # the next run.
    local _menu2_arming_mode
    _menu2_arming_mode="$(python3 ../../scripts/live_arming_mode.py \
        --bot "$_menu2_bot" decide --field mode 2>/dev/null | tr -d '\r')"
    if [ "$_menu2_arming_mode" = "disarmed" ]; then
        echo "${BOLD}-- live arming choice (menu 2 orchestrator) --${RESET}"
        echo "  [1] continue runtime-only start (NO TRADING)"
        echo "  [2] open menu 48 to approve supervised one-cycle or scheduled cron"
        echo "  [3] cancel start"
        read -r -p "Choose [1]: " _menu2_choice
        _menu2_choice="${_menu2_choice:-1}"
        case "$_menu2_choice" in
            1) ;;
            2) item_live_arming_mode ;;
            3) echo "${WARN}cancelled — no state changed${RESET}"; pause; return 0 ;;
            *) echo "${WARN}invalid choice, continuing runtime-only${RESET}" ;;
        esac
        echo
    fi

    # --- T-START-SOURCE-OF-TRUTH1 logging scaffold ---
    local _start_log='state/start.log.json'
    local _start_hist='state/start.log.jsonl'
    mkdir -p state 2>/dev/null || true
    local _generated_at _bot _mode _port _account_id _phase
    _generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    _bot="${START_BOT_LABEL:-unknown}"
    _port="$(configured_ibkr_port)"
    case "$_port" in
        7496|4001) _mode="live" ;;
        7497|4002) _mode="paper" ;;
        *)         _mode="unknown" ;;
    esac
    _account_id="$(printf '%s' "${LIVE_ACCOUNT_ID:-${IBKR_ACCOUNT_ID:-}}" | tr -d '[:space:]')"
    [ -z "$_account_id" ] && _account_id="unset"
    _phase="${LIVE_ROLLOUT_PHASE:-N/A}"
    # T-STARTLOG-DIAGNOSTIC-OVERRIDE-DIFF1 — split the previous
    # diagnostic_overrides allowlist into TWO surfaces:
    #
    #   env_snapshot         always-on observed values for the
    #                        config-ish vars (IBKR_PORT, IBKR_MODE,
    #                        LIVE_ROLLOUT_PHASE). Operator should NOT
    #                        read these as "diagnostic override".
    #
    #   diagnostic_overrides only values that materially differ from
    #                        the bot's .env baseline for the config-ish
    #                        vars, PLUS pure-test vars like
    #                        BROKER_WAIT_SECONDS when explicitly set.
    #                        These are what release_gate row 10 should
    #                        flag as lower-trust evidence.
    #
    # Allowlist excludes ALL credentials: IBKR_USERNAME, IBKR_PASSWORD,
    # IBKR_ACCOUNT_ID, LIVE_ACCOUNT_ID, LIVE_MODE_CONFIRM,
    # DASHBOARD_MUTATION_TOKEN. None of these are read.
    _env_baseline() {
        # Read a value from this bot's .env (cwd-local). Returns empty
        # when missing or unreadable.
        local _key="$1"
        if [ -f .env ]; then
            grep -E "^${_key}=" .env 2>/dev/null | head -1 \
                | cut -d= -f2- | tr -d '"' | tr -d "'" \
                | sed 's/^ *//; s/ *$//'
        fi
    }
    local _es_pack="" _do_pack=""
    local _es_key _es_val _es_baseline_val
    for _es_key in IBKR_PORT IBKR_MODE LIVE_ROLLOUT_PHASE; do
        eval "_es_val=\${$_es_key:-}"
        if [ -n "$_es_val" ]; then
            if [ -n "$_es_pack" ]; then
                _es_pack="${_es_pack};${_es_key}=${_es_val}"
            else
                _es_pack="${_es_key}=${_es_val}"
            fi
            _es_baseline_val="$(_env_baseline "$_es_key")"
            if [ -n "$_es_baseline_val" ] && [ "$_es_baseline_val" != "$_es_val" ]; then
                if [ -n "$_do_pack" ]; then
                    _do_pack="${_do_pack};${_es_key}=${_es_val}"
                else
                    _do_pack="${_es_key}=${_es_val}"
                fi
            fi
        fi
    done
    # BROKER_WAIT_SECONDS has no .env baseline — always an override when set.
    if [ -n "${BROKER_WAIT_SECONDS:-}" ]; then
        if [ -n "$_do_pack" ]; then
            _do_pack="${_do_pack};BROKER_WAIT_SECONDS=${BROKER_WAIT_SECONDS}"
        else
            _do_pack="BROKER_WAIT_SECONDS=${BROKER_WAIT_SECONDS}"
        fi
    fi
    local -a _steps_records=()
    _step_record() {
        # name, start, end, rc, status, error (opt), log_tail (opt, multi-line)
        # The multi-line log_tail is base64-encoded so newlines survive the
        # pipe-delimited line format used by _emit_start_log's tmpfile.
        local _n="$1" _s="$2" _e="$3" _r="$4" _st="$5"
        local _err="${6:-}" _tail="${7:-}"
        local _tail_b64=""
        if [ -n "$_tail" ]; then
            _tail_b64="$(printf '%s' "$_tail" | base64 -w 0 2>/dev/null \
                || printf '%s' "$_tail" | base64 | tr -d '\n')"
        fi
        _steps_records+=("$_n|$_s|$_e|$_r|$_st|$_err|$_tail_b64")
    }
    _emit_start_log() {
        # The python3 heredoc occupies stdin (for the script), so we
        # can't pipe step records in — instead we land them in a
        # tmpfile passed as argv (last positional) and read from there.
        # T-START-LOG-SEMANTICS1: schema_version=2 with start_kind /
        # cron_changed / runtime_prepared / direct_cycle_attempted.
        # T-BROKER-SESSION-EVIDENCE1: pack broker_session details into
        # arg 7 ($_bs_pack) — pipe-joined string of broker_runtime,
        # launch_attempted, launch_rc, wait_seconds, port_observation,
        # auth_state.
        # T-START-LOG-DASHBOARD-RESTORE-DETAILS1: arg 8 ($_dr_pack) packs
        # before/after dashboard listening state into
        # payload.dashboard_restore_details.
        local _final_status="$1" _final_summary="$2"
        local _start_kind="$3" _cron_changed="$4"
        local _runtime_prepared="$5" _direct_cycle_attempted="$6"
        local _bs_pack="${7:-}"
        local _dr_pack="${8:-}"
        local _do_pack="${9:-}"
        local _es_pack="${10:-}"
        local _steps_tmp
        _steps_tmp="$(mktemp /tmp/start_log_steps.XXXXXX 2>/dev/null || printf '/tmp/start_log_steps.%s' "$$")"
        if [ "${#_steps_records[@]}" -gt 0 ]; then
            printf '%s
' "${_steps_records[@]}" > "$_steps_tmp"
        else
            : > "$_steps_tmp"
        fi
        python3 - \
            "$_start_log" "$_start_hist" \
            "$_generated_at" "$_bot" "$_mode" "$_port" "$_account_id" \
            "$_phase" "$_final_status" "$_final_summary" \
            "$_start_kind" "$_cron_changed" "$_runtime_prepared" \
            "$_direct_cycle_attempted" "$_bs_pack" "$_dr_pack" "$_do_pack" "$_es_pack" "$_steps_tmp" <<'PY'
import json
import sys

(log, hist, generated_at, bot, mode, port, acct, phase,
 final_status, final_summary, start_kind, cron_changed,
 runtime_prepared, direct_cycle_attempted, bs_pack, dr_pack, do_pack,
 es_pack, steps_path) = sys.argv[1:20]


def _as_bool(s: str) -> bool:
    return s.strip().lower() == "true"


try:
    with open(steps_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
except OSError:
    lines = []
import base64

steps = []
for line in lines:
    if not line:
        continue
    parts = line.split('|', 6)
    if len(parts) < 5:
        continue
    name, start, end, rc, status = parts[:5]
    err = parts[5] if len(parts) >= 6 else ""
    tail_b64 = parts[6] if len(parts) >= 7 else ""
    try:
        rc_val = int(rc)
    except ValueError:
        rc_val = None
    step = {
        "name": name,
        "start": start,
        "end": end,
        "rc": rc_val,
        "status": status,
    }
    if err:
        step["error"] = err
    if tail_b64:
        try:
            step["log_tail"] = base64.b64decode(tail_b64).decode("utf-8", errors="replace")
        except Exception:
            pass
    steps.append(step)
try:
    port_val = int(port)
except ValueError:
    port_val = None
bs_details = None
if bs_pack:
    bs_parts = bs_pack.split('|')
    if len(bs_parts) >= 6:
        def _i(s):
            try:
                return int(s)
            except (TypeError, ValueError):
                return None
        # T-START-LOG-BROKER-UP-AFTER-BLOCK1 — field 7 is the post-block
        # recheck observation; null on happy paths.
        recheck_raw = bs_parts[6] if len(bs_parts) >= 7 else ""
        bs_details = {
            "broker_runtime": bs_parts[0] or None,
            "launch_attempted": _as_bool(bs_parts[1]),
            "launch_rc": _i(bs_parts[2]),
            "wait_seconds": _i(bs_parts[3]),
            "port_observation": bs_parts[4] or None,
            "auth_state": bs_parts[5] or None,
            "broker_recheck_after_block": recheck_raw or None,
        }
# T-START-LOG-RETENTION-COMPACT1 — keep last KEEP lines of the JSONL
# history; archive the previous full file when the threshold trips.
# Computed BEFORE writing start.log.json so the retention result is
# part of the JSON payload itself (same write).
import datetime as _dt2
import os

KEEP = 200
jsonl_retention = {"kept_lines": None, "archive": None}
existing_lines: list[str] = []
try:
    if os.path.exists(hist):
        with open(hist, "r", encoding="utf-8") as _hf:
            existing_lines = _hf.readlines()
except OSError:
    existing_lines = []
projected = len(existing_lines) + 1  # +1 for the line we are about to append
archive_path: str | None = None
if projected > KEEP:
    ts = _dt2.datetime.now(_dt2.UTC).strftime("%Y%m%d-%H%M%S")
    archive_dir = os.path.join(os.path.dirname(hist) or ".", "archive")
    try:
        os.makedirs(archive_dir, exist_ok=True)
    except OSError:
        archive_dir = ""
    if archive_dir:
        archive_path = os.path.join(archive_dir, f"start.log.{ts}.jsonl")
    jsonl_retention = {"kept_lines": KEEP, "archive": archive_path}
else:
    jsonl_retention = {"kept_lines": projected, "archive": None}

# T-START-LOG-DASHBOARD-RESTORE-DETAILS1 — unpack dr_pack into a
# structured details object so Diagnostics can read before/after port
# state without re-parsing the bash array.
dashboard_restore_details = None
if dr_pack:
    _dr_parts = dr_pack.split('|')
    if len(_dr_parts) >= 4:
        _pre_v12 = _as_bool(_dr_parts[0])
        _pre_opt = _as_bool(_dr_parts[1])
        _post_v12 = _as_bool(_dr_parts[2])
        _post_opt = _as_bool(_dr_parts[3])
        _restored_raw = _dr_parts[4] if len(_dr_parts) >= 5 else ""
        _restored_list = [s for s in _restored_raw.split(',') if s]
        dashboard_restore_details = {
            "before":   {"v1.2": _pre_v12, "options-v1.2": _pre_opt},
            "after":    {"v1.2": _post_v12, "options-v1.2": _post_opt},
            "restored": _restored_list,
        }

# T-RELEASE-GATE-STARTLOG-TEST-OVERRIDE-WARNING1 +
# T-STARTLOG-DIAGNOSTIC-OVERRIDE-DIFF1 — unpack two distinct
# allowlists. env_snapshot captures the always-on config vars (never a
# warning surface). diagnostic_overrides only carries values that
# differ from baseline or are pure-test vars.
def _parse_kv_pack(pack):
    out = {}
    if not pack:
        return None
    for _kv in pack.split(';'):
        if '=' in _kv:
            _k, _, _v = _kv.partition('=')
            if _k and _v:
                out[_k] = _v
    return out or None

env_snapshot = _parse_kv_pack(es_pack)
diagnostic_overrides = _parse_kv_pack(do_pack)

# T-MENU46-CLEAR-MARK-ON-NORMAL-START1 — preserve diagnostic supersede
# markers from the existing start.log.json ONLY when this menu 2 run
# did NOT reach a clean state. The operator's mark stays load-bearing
# until a real successful start replaces it. On final_status="ok" we
# read nothing and the new payload naturally lacks the fields ->
# marker clears.
preserved_diag = {}
if final_status != "ok":
    try:
        with open(log, "r", encoding="utf-8") as _ef:
            _existing = json.load(_ef)
        if isinstance(_existing, dict):
            for _key in (
                "diagnostic_test_superseded",
                "diagnostic_test_superseded_at",
                "diagnostic_test_reason",
            ):
                if _key in _existing:
                    preserved_diag[_key] = _existing[_key]
    except (OSError, json.JSONDecodeError):
        pass

payload = {
    "schema_version": 2,
    "menu_action_id": "item_start",
    "generated_at": generated_at,
    "bot": bot,
    "mode": mode,
    "dry_run": False,
    "one_cycle": False,
    "phase": phase,
    "account_id": acct,
    "port": port_val,
    "start_kind": start_kind,
    "cron_changed": _as_bool(cron_changed),
    "runtime_prepared": _as_bool(runtime_prepared),
    "direct_cycle_attempted": _as_bool(direct_cycle_attempted),
    "broker_session_details": bs_details,
    "steps": steps,
    "final_status": final_status,
    "final_summary": final_summary,
    "dashboard_restore_details": dashboard_restore_details,
    "env_snapshot": env_snapshot,
    "diagnostic_overrides": diagnostic_overrides,
    "jsonl_retention": jsonl_retention,
}
# T-MENU46-CLEAR-MARK-ON-NORMAL-START1 — merge preserved diagnostic
# markers (BLOCKED/FAILED only; empty dict on SUCCESS).
if preserved_diag:
    payload.update(preserved_diag)
try:
    with open(log, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2))
except OSError:
    pass

# T-MENU2-STALE-TWS-SIDECAR-CLEAR1 — mark stale TWS exit evidence as
# superseded ONLY when this menu 2 run reached a broker-ready state.
# Never fires on the BLOCKED / failed paths (start_kind in those cases
# is "blocked" or "failed"). Never deletes the sidecar; appends two
# fields so the audit trail stays intact.
if start_kind in ("cron_enabled", "runtime_only"):
    _ts_sidecar = os.path.join(os.path.dirname(log) or ".", "tws_exit_signal.json")
    if os.path.exists(_ts_sidecar):
        try:
            with open(_ts_sidecar, "r", encoding="utf-8") as _sf:
                _sc = json.load(_sf)
            if isinstance(_sc, dict):
                _sc["superseded_by_menu2_start_at"] = generated_at
                _sc["current_broker_status"] = "listening"
                with open(_ts_sidecar, "w", encoding="utf-8") as _sf:
                    json.dump(_sc, _sf, indent=2)
        except (OSError, json.JSONDecodeError):
            pass

new_line = json.dumps(payload) + "\n"
if archive_path:
    try:
        with open(archive_path, "w", encoding="utf-8") as _af:
            _af.writelines(existing_lines)
        # Keep last (KEEP - 1) of existing + the new line = KEEP total
        kept = existing_lines[-(KEEP - 1):] if KEEP > 1 else []
        with open(hist, "w", encoding="utf-8") as f:
            f.writelines(kept)
            f.write(new_line)
    except OSError:
        # Best-effort fallback: append without retention rather than lose
        # the new line.
        try:
            with open(hist, "a", encoding="utf-8") as f:
                f.write(new_line)
        except OSError:
            pass
else:
    try:
        with open(hist, "a", encoding="utf-8") as f:
            f.write(new_line)
    except OSError:
        pass
PY
        rm -f "$_steps_tmp" 2>/dev/null || true
    }
    _print_start_summary() {
        local _final_status="$1" _final_summary="$2" _start_kind="$3"
        local _overall_label="FAILED" _overall_color="$FAIL"
        case "$_final_status" in
            ok)      _overall_label="READY";   _overall_color="$OK" ;;
            blocked) _overall_label="BLOCKED"; _overall_color="$WARN" ;;
            failed)  _overall_label="FAILED";  _overall_color="$FAIL" ;;
        esac
        echo
        echo "${BOLD}── menu 2 SUMMARY ──${RESET}"
        echo "  OVERALL:    ${_overall_color}${_overall_label}${RESET}"
        echo "  start_kind: ${_start_kind}"
        echo "  bot:        $_bot"
        echo "  mode:       $_mode"
        echo "  account:    $_account_id"
        echo "  port:       $_port"
        echo "  phase:      $_phase"
        echo "  steps:"
        local _rec _name _start _end _rc _status _err _tail_b64
        for _rec in "${_steps_records[@]}"; do
            IFS='|' read -r _name _start _end _rc _status _err _tail_b64 <<< "$_rec"
            echo "    ${_name}  rc=${_rc}  ${_status}"
        done
        echo "  detail:     ${_final_summary}"
        echo "  log:        $_start_log"
        echo "              $_start_hist  (history)"
    }
    _print_artifact_review() {
        # T-MENU2-ARTIFACT-REVIEW-BLOCK1 — compact source-of-truth pointer
        # printed after the existing ── menu 2 SUMMARY ── on BOTH BLOCKED
        # and OK paths. The block names the JSON + JSONL paths and the
        # one-line "review first" pointer so a paste contains everything
        # Diagnostics needs to find the artifact. Pure render; does not
        # touch the JSON itself.
        local _final_status="$1" _start_kind="$2"
        local _dr_status="" _rec _n _s _e _r _st _err _tb
        for _rec in "${_steps_records[@]}"; do
            IFS='|' read -r _n _s _e _r _st _err _tb <<< "$_rec"
            if [ "$_n" = "dashboard_restore" ]; then
                _dr_status="$_st"
            fi
        done
        echo
        echo "${DIM}-- artifact review --${RESET}"
        echo "  latest:        $_start_log"
        echo "  history:       $_start_hist"
        echo "  generated_at:  $_generated_at"
        echo "  final_status:  $_final_status"
        echo "  start_kind:    $_start_kind"
        if [ -n "$_dr_status" ]; then
            echo "  dashboard_restore: $_dr_status"
        fi
        echo "  ${DIM}diagnostics: review this file first before log tails${RESET}"
    }
    # T-START-SCREEN-DETAIL1 + T-START-LOG-DETAILS1 ---------------------------
    # Failure-only context. _collect_why_* returns multi-line text that is
    # both rendered on screen via _print_why_block AND attached to the
    # failing step's log_tail field in state/start.log.json.
    _collect_why_broker_blocked() {
        # Args 3..9 carry the structured broker_session_details captured
        # in the broker_session step. They render here for the on-screen
        # ── why ── block; the same values also persist via _emit_start_log.
        # Arg 9 = T-START-LOG-BROKER-UP-AFTER-BLOCK1 recheck observation.
        local _target="$1" _other="$2"
        local _br="${3:-}" _la="${4:-}" _lrc="${5:-}" _ws="${6:-}"
        local _po="${7:-}" _as="${8:-}" _recheck="${9:-}"
        printf 'failed_step:    broker_session\n'
        printf 'rc:             1\n'
        printf 'error:          broker_not_ready: target_port=:%s not listening' "$_target"
        if [ -n "$_other" ]; then
            printf '; other_ports=%s' "$_other"
        fi
        printf '\nbroker_session_details:\n'
        printf '  broker_runtime:              %s\n' "${_br:-(unset)}"
        printf '  launch_attempted:            %s\n' "${_la:-false}"
        printf '  launch_rc:                   %s\n' "${_lrc:-(n/a)}"
        printf '  wait_seconds:                %s\n' "${_ws:-0}"
        printf '  port_observation:            %s\n' "${_po:-(unset)}"
        printf '  auth_state:                  %s\n' "${_as:-(unset)}"
        printf '  broker_recheck_after_block:  %s\n' "${_recheck:-(unset)}"
        printf 'broker_ports_listening:\n'
        local _lp
        _lp="$(broker_ports_list 2>/dev/null)"
        if [ -n "$_lp" ]; then
            printf '  %s\n' "$_lp"
        else
            printf '  (none)\n'
        fi
        printf 'processes_on_broker_ports:\n'
        local _ports_proc
        _ports_proc="$(ss -tlnp 2>/dev/null | grep -E ':(4001|4002|7496|7497)\b' | head -6)"
        if [ -n "$_ports_proc" ]; then
            printf '%s\n' "$_ports_proc" | sed 's/^/  /'
        else
            printf '  (none)\n'
        fi
        printf 'broker_log_tails:\n'
        local _f _t any=0
        for _f in state/tws.log state/ibgateway.log; do
            if [ -f "$_f" ]; then
                any=1
                printf '  %s (last 8 lines):\n' "$_f"
                _t="$(tail -n 8 "$_f" 2>/dev/null | sed 's/^/    /')"
                printf '%s\n' "$_t"
            fi
        done
        [ "$any" -eq 0 ] && printf '  (no broker logs at state/tws.log or state/ibgateway.log)\n'
        printf 'next_steps:\n'
        printf '  - if no broker GUI is visible, launch TWS via menu 44 (or IB Gateway)\n'
        printf '  - complete username/password + 2FA in the broker window\n'
        printf '  - if broker is up on the WRONG port, switch its login mode\n'
        printf '  - during a TWS restart / re-auth, a ~30-60s BLOCKED window is\n'
        printf '    expected — wait it out and re-run menu 2, do NOT classify as defect\n'
        printf '  - re-run menu 2 once :%s is listening (this wrapper will\n' "$_target"
        printf '    auto-wait up to BROKER_WAIT_SECONDS=${BROKER_WAIT_SECONDS:-180}s)\n'
    }
    _collect_why_bot_enable_failed() {
        local _rc="$1"
        printf 'failed_step:    bot_enable\n'
        printf 'rc:             %s\n' "$_rc"
        printf 'error:          refresh_selected_bot_runtime returned rc=%s\n' "$_rc"
        printf 'bot_log_tails:\n'
        local _f _t any=0
        for _f in state/bot.log state/bot_control.log state/cron.log state/dashboard.log; do
            if [ -f "$_f" ]; then
                any=1
                printf '  %s (last 8 lines):\n' "$_f"
                _t="$(tail -n 8 "$_f" 2>/dev/null | sed 's/^/    /')"
                printf '%s\n' "$_t"
            fi
        done
        [ "$any" -eq 0 ] && printf '  (no bot logs at state/bot.log, state/bot_control.log, state/cron.log, state/dashboard.log)\n'
        printf 'concurrent_uvicorn_processes:\n'
        local _uv
        _uv="$(ps -eo pid,cmd 2>/dev/null | grep -E 'uvicorn.*(dashboard|app:app)' | grep -v grep | head -4)"
        if [ -n "$_uv" ]; then
            printf '%s\n' "$_uv" | sed 's/^/  /'
        else
            printf '  (none)\n'
        fi
        printf 'next_steps:\n'
        printf '  - check state/bot.log or state/bot_control.log for the failing line\n'
        printf '  - verify cron entries via menu 28 (install/refresh crontab)\n'
        printf '  - re-run menu 2 after the underlying issue is fixed\n'
    }
    _print_why_block() {
        local _why_text="$1"
        [ -z "$_why_text" ] && return 0
        echo
        echo "${BOLD}── why ──${RESET}"
        printf '%s\n' "$_why_text" | sed 's/^/  /'
    }

    # Step 1/2: broker session
    local _s1_start _s1_end _s1_rc=0 _s1_status="ok"
    _s1_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    echo "${BOLD}── 1/2: broker session ──${RESET}"
    # T-TWS-LIVE-AUTOLOGIN-STARTUP-AUTOMATION1 — when the operator has
    # installed the TWS-live autopilot wrapper (~/ibgateway/tws1-live)
    # AND we're starting a live session, consult its status and offer
    # to drive an automatic relaunch + 2FA checkpoint before falling
    # through to the existing port-wait loop. Read-only by default;
    # the wrapper is only invoked when the operator types y.
    if [ -x "$HOME/ibgateway/tws1-live" ] && is_live_env_mode; then
        echo
        echo "${BOLD}-- TWS-live autopilot status --${RESET}"
        python3 ../../scripts/tws_autologin.py --bot "$_menu2_bot" status 2>/dev/null \
            || echo "${WARN}autopilot status helper failed${RESET}"
        # Snapshot status for branching. Read-only.
        local _autopilot_status
        _autopilot_status="$(python3 ../../scripts/tws_autologin.py \
            --bot "$_menu2_bot" status --json 2>/dev/null \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status",""))' 2>/dev/null \
            | tr -d '\r')"
        case "$_autopilot_status" in
            ready)
                echo "${OK}autopilot reports ready — broker port should be listening already${RESET}"
                ;;
            awaiting_2fa)
                echo
                echo "${WARN}AUTOPILOT AWAITING 2FA — approve in your IBKR Mobile app or${RESET}"
                echo "${WARN}enter the second-factor code in the TWS GUI now. menu 2 will${RESET}"
                echo "${WARN}wait below for the port to come up.${RESET}"
                read -r -p "Press Enter once you have completed the 2FA challenge..." _2fa_ack
                ;;
            timed_out|permission_refused|install_missing|ibc_missing|unknown)
                echo
                echo "${WARN}autopilot status=$_autopilot_status — automated relaunch unavailable.${RESET}"
                echo "${DIM}Fallback: menu 44 (Launch TWS only) + GUI login.${RESET}"
                ;;
            no_evidence|dry_run_ok|"")
                echo
                read -r -p "Run TWS-live autopilot relaunch now? (y/N): " _ap_yn
                case "$_ap_yn" in
                    y|Y|yes|YES)
                        echo "${BOLD}-- launching ~/ibgateway/tws1-live --confirm --${RESET}"
                        python3 ../../scripts/tws_autologin.py --bot "$_menu2_bot" \
                            launch --confirm 2>&1 | head -30
                        # If the wrapper says it's awaiting 2FA, hand off
                        # to the operator and wait for their go-ahead.
                        local _post_ap_status
                        _post_ap_status="$(python3 ../../scripts/tws_autologin.py \
                            --bot "$_menu2_bot" status --json 2>/dev/null \
                            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status",""))' 2>/dev/null \
                            | tr -d '\r')"
                        if [ "$_post_ap_status" = "awaiting_2fa" ]; then
                            echo
                            echo "${WARN}AUTOPILOT AWAITING 2FA — complete the challenge now.${RESET}"
                            read -r -p "Press Enter once 2FA is approved..." _2fa_ack
                        fi
                        ;;
                    *)
                        echo "${DIM}skipped — falling through to manual broker session wait${RESET}"
                        ;;
                esac
                ;;
        esac
        # Persist the autopilot snapshot into the bot's state so
        # _emit_start_log can pick it up alongside its other packs.
        python3 ../../scripts/tws_autologin.py --bot "$_menu2_bot" embed \
            >/dev/null 2>&1 || true
        echo
    fi
    local broker_ready=0
    local target_port runtime_name other_ports
    local launched_runtime=0
    target_port="$(configured_ibkr_port)"
    other_ports=""
    case "$target_port" in
        7496|7497) runtime_name="TWS" ;;
        4001|4002) runtime_name="IB Gateway" ;;
        *)         runtime_name="broker session" ;;
    esac

    echo "  target runtime: ${BOLD}${runtime_name}${RESET} on :$target_port"
    # T-BROKER-SESSION-EVIDENCE1 — structured evidence captured during this
    # phase so a blocked broker_session can be classified from start.log.json
    # alone without paging the operator for ss / tail / pgrep follow-ups.
    local _broker_runtime="$runtime_name"
    local _launch_attempted="false"
    local _launch_rc=""
    local _wait_seconds=0
    local _port_observation=""
    local _auth_state=""
    if port_listening "$target_port"; then
        echo "  ${OK}✓${RESET} target port already listening: :$target_port"
        broker_ready=1
        _port_observation="listening_from_start"
        _auth_state="no_action_needed"
    else
        other_ports="$(broker_ports_list)"
        if [ -n "$other_ports" ]; then
            echo "  ${WARN}!${RESET} other broker port(s) already listening:${other_ports}"
            if [ "$runtime_name" = "TWS" ] && echo "$other_ports" | grep -qE ':(4001|4002)\b'; then
                if kill_gateway_runtime; then
                    other_ports="$(broker_ports_list)"
                fi
            fi
        fi

        if port_listening "$target_port"; then
            echo "  ${OK}✓${RESET} target port now listening: :$target_port"
            broker_ready=1
            _port_observation="listening_from_start"
            _auth_state="no_action_needed"
        elif [ -n "$other_ports" ]; then
            echo "  ${DIM}This bot expects :$target_port. Wrong-session drift is still present;${RESET}"
            echo "  ${DIM}clear the other broker runtime, then re-run.${RESET}"
            _port_observation="wrong_session_drift"
            _auth_state="wrong_session_drift"
        else
            _launch_attempted="true"
            if [ "$runtime_name" = "TWS" ]; then
                echo "  target port down — launching TWS"
                launch_tws_runtime
                _launch_rc=$?
            else
                echo "  target port down — launching IB Gateway"
                launch_gateway_runtime
                _launch_rc=$?
            fi
            if [ "$_launch_rc" -eq 0 ]; then
                launched_runtime=1
            fi
        fi

        # T-TWS-AUTH-AUTOMATION1 — operator-checkpoint handoff. The wait
        # loop is now long enough to absorb a 2FA flow, prints periodic
        # progress so the operator knows we're still alive, and escalates
        # to an explicit OPERATOR CHECKPOINT banner at 60s elapsed. No
        # credential reads. No IBC config edits. The broker GUI is the
        # auth surface — this wrapper just polls and reports.
        if [ "$broker_ready" -ne 1 ] && [ "$launched_runtime" -eq 1 ]; then
            local _wait_max="${BROKER_WAIT_SECONDS:-180}"
            echo "  waiting up to ${_wait_max}s for :$target_port to open..."
            local i _checkpoint_announced=0
            for i in $(seq 1 "$_wait_max"); do
                if port_listening "$target_port"; then
                    echo "  ${OK}✓${RESET} target port up after ${i}s"
                    broker_ready=1
                    _wait_seconds="$i"
                    _port_observation="came_up_after_launch"
                    _auth_state="ready"
                    break
                fi
                if [ "$i" -eq 60 ] && [ "$_checkpoint_announced" -eq 0 ]; then
                    _checkpoint_announced=1
                    echo
                    echo "  ${WARN}OPERATOR CHECKPOINT${RESET} — broker login appears pending."
                    echo "  Complete username/password + 2FA in the ${_broker_runtime} window."
                    echo "  This wrapper will keep polling :${target_port} for"
                    echo "  another $((_wait_max - i))s. Ctrl-C aborts cleanly."
                    echo
                elif [ $((i % 15)) -eq 0 ] && [ "$i" -lt "$_wait_max" ]; then
                    echo "  ${DIM}waiting... ${i}s elapsed, :${target_port} not yet listening${RESET}"
                fi
                sleep 1
            done
            if [ "$broker_ready" -ne 1 ]; then
                _wait_seconds="$_wait_max"
                _port_observation="never_listened"
                _auth_state="timed_out_at_login"
                echo "  ${WARN}!${RESET} target port :$target_port still not listening after ${_wait_max}s"
                echo "  ${DIM}Most common cause: GUI login still pending or wrong login mode selected.${RESET}"
                echo "  ${DIM}Complete the broker login window, then re-run this menu.${RESET}"
            fi
        fi
    fi
    _s1_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local _s1_error="" _s1_log_tail=""
    # T-START-LOG-BROKER-UP-AFTER-BLOCK1 — one final port_listening probe
    # on the BLOCKED path so the operator can see whether the TWS 30-60s
    # login/re-auth race brought :target_port up shortly after our wait
    # closed. No launch. No extended wait. Single read.
    local _broker_recheck_after_block=""
    if [ "$broker_ready" -eq 1 ]; then
        _s1_rc=0; _s1_status="ok"
    else
        if port_listening "$target_port"; then
            _broker_recheck_after_block="came_up_after_timeout"
        else
            _broker_recheck_after_block="still_down"
        fi
        _s1_rc=1; _s1_status="broker_not_ready"
        _s1_error="broker_not_ready: target_port=:${target_port} not listening"
        _s1_log_tail="$(_collect_why_broker_blocked "$target_port" "$other_ports" \
            "$_broker_runtime" "$_launch_attempted" "$_launch_rc" \
            "$_wait_seconds" "$_port_observation" "$_auth_state" \
            "$_broker_recheck_after_block")"
    fi
    # T-BROKER-SESSION-EVIDENCE1 + T-START-LOG-BROKER-UP-AFTER-BLOCK1 — pack
    # the structured evidence into one pipe-joined string so _emit_start_log
    # can pass it through to the python heredoc as a single positional arg.
    # Field 7 (broker_recheck_after_block) is empty on the success path so
    # the JSON renders null/omitted for happy starts.
    local _bs_pack="${_broker_runtime}|${_launch_attempted}|${_launch_rc}|${_wait_seconds}|${_port_observation}|${_auth_state}|${_broker_recheck_after_block}"
    _step_record "broker_session" "$_s1_start" "$_s1_end" "$_s1_rc" \
        "$_s1_status" "$_s1_error" "$_s1_log_tail"
    echo

    echo "${BOLD}── 2/2: bot enable ──${RESET}"
    if [ "$broker_ready" -ne 1 ]; then
        echo "  ${FAIL}✗${RESET} START BLOCKED — broker session not ready"
        echo "  ${DIM}Bot cron NOT modified. Once the expected broker port is${RESET}"
        echo "  ${DIM}listening on :$target_port, re-run this Start menu.${RESET}"
        # T-TWS-RESTART-WINDOW-NOTE1 — explanatory note, NOT a behavior change.
        echo "  ${DIM}Note: during a TWS restart / re-auth window, BLOCKED is${RESET}"
        echo "  ${DIM}expected for ~30-60s while the broker session rebuilds.${RESET}"
        echo "  ${DIM}If you are not actively cutting TWS over, treat this as a${RESET}"
        echo "  ${DIM}real broker outage and inspect the ── why ── block below.${RESET}"
        local _s2_skip_ts
        _s2_skip_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        _step_record "bot_enable" "$_s2_skip_ts" "$_s2_skip_ts" 1 \
            "skipped_broker_blocked" \
            "broker session not ready; bot_enable skipped" \
            ""
        _emit_start_log "blocked" "broker session not ready on :$target_port" \
            "blocked" "false" "false" "false" "$_bs_pack" "" "$_do_pack" "$_es_pack"
        _print_why_block "$_s1_log_tail"
        _print_start_summary "blocked" "broker session not ready on :$target_port" "blocked"
        _print_artifact_review "blocked" "blocked"
        echo
        echo "  ${DIM}start log: $_start_log${RESET}"
        pause
        return
    fi
    if is_live_env_mode; then
        echo "  ${DIM}Live mode detected: this menu prepares broker + dashboard runtime${RESET}"
        echo "  ${DIM}but does NOT run main.py and does NOT arm a live cron path.${RESET}"
        echo "  ${DIM}For any supervised live cycle, use menu 41 (LIVE LAUNCHER WIZARD).${RESET}"
    else
        echo "  ${DIM}Note: this enables the bot's cron schedule. The bot will${RESET}"
        echo "  ${DIM}run on the next scheduled tick, not immediately. For a${RESET}"
        echo "  ${DIM}one-cycle live run, use menu 41 (LIVE LAUNCHER WIZARD)${RESET}"
        echo "  ${DIM}instead.${RESET}"
    fi
    # T-MENU2-ONE-COMMAND-LIVE-START-ORCHESTRATOR1 — internal hand-off
    # to menu 41 (supervised) or menu 28 (scheduled) when the operator
    # has armed the corresponding mode but the activation evidence is
    # still missing. We prompt before calling so menu 2 never silently
    # runs the launcher / installer; both downstream menus keep their
    # own confirmation gates.
    local _menu2_arm_now
    _menu2_arm_now="$(python3 ../../scripts/live_arming_mode.py \
        --bot "$_menu2_bot" decide --field mode 2>/dev/null | tr -d '\r')"
    if [ "$_menu2_arm_now" = "supervised_one_cycle" ] \
       && [ ! -f state/live_launcher.json ]; then
        echo "${BOLD}-- supervised one-cycle pending --${RESET}"
        echo "  ${DIM}arming_mode=supervised_one_cycle and no menu 41 evidence yet${RESET}"
        # T-MENU2-SUPERVISED-LIVE-RUN-NO-DRYRUN-TRAP1 — call out the
        # dry-run trap explicitly: the wizard's default Dry-run prompt
        # answer is "y", and dry-run does NOT run main.py, NOT write
        # account_summary, and NOT clear the stale dashboard banner.
        # The TODO row stays NEEDED until a REAL one-cycle completes.
        echo
        echo "${WARN}IMPORTANT — menu 41 defaults to PREVIEW ONLY (dry-run).${RESET}"
        echo "${WARN}A dry-run WILL NOT FIX STALE DATA OR RUN THE BOT.${RESET}"
        echo "${WARN}For a real supervised live cycle:${RESET}"
        echo "${DIM}  - choose mode 2 (live)${RESET}"
        echo "${DIM}  - answer N (or no) to the Dry-run prompt${RESET}"
        echo "${DIM}  - confirm --one-cycle when prompted${RESET}"
        echo "${DIM}  - type the canonical phrase exactly:${RESET}"
        echo "${DIM}      ENABLE LIVE TRADING ON ACCOUNT <your account id>${RESET}"
        echo
        read -r -p "Open menu 41 (LIVE LAUNCHER) now? [y/N]: " _menu2_yn41
        case "$_menu2_yn41" in
            y|Y) item_live_launcher_wizard ;;
            *)  echo "${DIM}skipped — operator can run menu 41 later${RESET}" ;;
        esac
    elif [ "$_menu2_arm_now" = "scheduled_cron" ] \
       && { [ ! -f state/cron_armed.json ] \
            || ! grep -q '"main_py_cron_active": true' state/cron_armed.json 2>/dev/null; }; then
        echo "${BOLD}-- scheduled cron activation pending --${RESET}"
        echo "  ${DIM}arming_mode=scheduled_cron but cron lines are still commented${RESET}"
        read -r -p "Open menu 28 (install/refresh crontab) now? [y/N]: " _menu2_yn28
        case "$_menu2_yn28" in
            y|Y) item_install_crontab ;;
            *)  echo "${DIM}skipped — operator can run menu 28 later${RESET}" ;;
        esac
    fi
    echo
    local _s2_start _s2_end _s2_rc=0 _s2_status="ok"
    _s2_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "  switching to ${START_BOT_LABEL} and refreshing its session..."
    if refresh_selected_bot_runtime; then
        _s2_rc=0; _s2_status="ok"
        if is_live_env_mode; then
            echo "  ${OK}✓${RESET} ${START_BOT_LABEL} runtime prepared"
        else
            echo "  ${OK}✓${RESET} ${START_BOT_LABEL} enabled"
        fi
    else
        _s2_rc=1; _s2_status="refresh_failed"
        echo "  ${FAIL}✗${RESET} failed to enable ${START_BOT_LABEL}"
    fi
    _s2_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local _s2_error="" _s2_log_tail=""
    if [ "$_s2_rc" -ne 0 ]; then
        _s2_error="refresh_selected_bot_runtime returned rc=${_s2_rc}"
        _s2_log_tail="$(_collect_why_bot_enable_failed "$_s2_rc")"
    fi
    _step_record "bot_enable" "$_s2_start" "$_s2_end" "$_s2_rc" \
        "$_s2_status" "$_s2_error" "$_s2_log_tail"

    # T-MENU2-DASHBOARD-PRESERVE-OTHER1 — refresh_selected_bot_runtime
    # stops the OTHER bot via bot_control.sh, which also kills the OTHER
    # bot's uvicorn dashboard. Restore both dashboards via
    # tools/dashboard_runtime.py start (idempotent — already-running
    # dashboards are skipped). bot_control's exclusivity semantics are
    # preserved (other bot's main.py + cron remain stopped); only the
    # dashboard side-effect is undone. Recorded as a step so the SUMMARY
    # block and start.log.json reflect the recovery.
    local _dr_start _dr_end _dr_rc=0 _dr_status="skipped"
    local _dr_error="" _dr_log_tail=""
    # T-START-LOG-DASHBOARD-RESTORE-DETAILS1 — capture per-port listening
    # state before + after the dashboard_runtime start call, so
    # Diagnostics can see exactly which dashboard the step restored.
    local _dr_pre_v12="false" _dr_pre_opt="false"
    local _dr_post_v12="false" _dr_post_opt="false"
    if port_listening 8080; then _dr_pre_v12="true"; fi
    if port_listening 8082; then _dr_pre_opt="true"; fi
    _dr_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -f ../../tools/dashboard_runtime.py ]; then
        echo "  restoring dashboards (idempotent; other bot's dashboard may have been stopped)..."
        python3 ../../tools/dashboard_runtime.py start 2>&1 | sed 's/^/    /' || _dr_rc=$?
        if [ "$_dr_rc" -eq 0 ]; then
            _dr_status="ok"
        else
            _dr_status="restore_failed"
            _dr_error="tools/dashboard_runtime.py start returned rc=${_dr_rc}"
            echo "  ${WARN}!${RESET} dashboard restore returned rc=${_dr_rc} (selected bot still healthy)"
        fi
    else
        _dr_error="tools/dashboard_runtime.py not present"
    fi
    _dr_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if port_listening 8080; then _dr_post_v12="true"; fi
    if port_listening 8082; then _dr_post_opt="true"; fi
    # Restored list: bots that went from no -> yes during this step.
    local _dr_restored=""
    if [ "$_dr_pre_v12" = "false" ] && [ "$_dr_post_v12" = "true" ]; then
        _dr_restored="v1.2"
    fi
    if [ "$_dr_pre_opt" = "false" ] && [ "$_dr_post_opt" = "true" ]; then
        if [ -n "$_dr_restored" ]; then _dr_restored="${_dr_restored},options-v1.2"
        else _dr_restored="options-v1.2"; fi
    fi
    _step_record "dashboard_restore" "$_dr_start" "$_dr_end" "$_dr_rc" \
        "$_dr_status" "$_dr_error" "$_dr_log_tail"
    # Packed details forwarded to the python heredoc as a NEW positional
    # arg. The heredoc unpacks into payload.dashboard_restore_details.
    local _dr_pack="${_dr_pre_v12}|${_dr_pre_opt}|${_dr_post_v12}|${_dr_post_opt}|${_dr_restored}"
    local _final_status="ok"
    local _final_summary="bot enabled; cron will run on next scheduled tick"
    if is_live_env_mode; then
        _final_summary="runtime ready; live cron unchanged; use menu 41 for supervised live cycles"
    fi
    if [ "$_s2_rc" -ne 0 ]; then
        _final_status="failed"
        _final_summary="bot_enable failed (refresh_selected_bot_runtime returned non-zero)"
    fi
    local _start_kind="cron_enabled"
    if is_live_env_mode; then
        _start_kind="runtime_only"
    fi
    local _cron_changed="false"
    if [ "$_start_kind" = "cron_enabled" ] && [ "$_s2_rc" -eq 0 ]; then
        _cron_changed="true"
    fi
    local _runtime_prepared="false"
    if [ "$_s2_rc" -eq 0 ] && [ "$broker_ready" -eq 1 ]; then
        _runtime_prepared="true"
    fi
    if [ "$_final_status" != "ok" ]; then
        _start_kind="failed"
    fi
    _emit_start_log "$_final_status" "$_final_summary" \
        "$_start_kind" "$_cron_changed" "$_runtime_prepared" "false" "$_bs_pack" "$_dr_pack" "$_do_pack" "$_es_pack"
    # T-MENU2-LIVE-START-ORCHESTRATOR-VISIBLE-TODO1 — embed the
    # post-action TODO board into start.log.json so Diagnostics reads
    # the same view from the canonical artifact, and re-render it to
    # the terminal as part of the unified SUMMARY so the operator
    # ends the run with one clear "what's still needed" block.
    if [ -f ../../scripts/menu2_todo_board.py ]; then
        python3 ../../scripts/menu2_todo_board.py \
            --bot "$_menu2_bot" \
            --probe-host "${IBKR_HOST:-127.0.0.1}" \
            --probe-port "$(configured_ibkr_port)" \
            embed >/dev/null 2>&1 || true
    fi
    # T-TWS-LIVE-AUTOLOGIN-STARTUP-AUTOMATION1 — fold the autopilot
    # snapshot into the same canonical start.log.json so Diagnostics
    # sees broker_login_automation alongside the rest of the start
    # evidence without grepping a separate sidecar.
    if [ -f ../../scripts/tws_autologin.py ]; then
        python3 ../../scripts/tws_autologin.py --bot "$_menu2_bot" \
            embed-into-start-log >/dev/null 2>&1 || true
    fi
    # T-MENU2-REAL-CYCLE-POSTRUN-EVIDENCE-CHECK1 — after a (potentially)
    # real menu 41 cycle, verify every expected post-cycle artefact is
    # on disk and fresh. Prints a verdict block to the terminal and
    # mirrors it into start.log.json so Diagnostics reads it from the
    # canonical artefact. Read-only.
    if [ -f ../../scripts/realcycle_postrun_check.py ]; then
        echo
        python3 ../../scripts/realcycle_postrun_check.py --bot "$_menu2_bot" print \
            2>/dev/null || echo "${WARN}realcycle_postrun_check helper failed${RESET}"
        python3 ../../scripts/realcycle_postrun_check.py --bot "$_menu2_bot" \
            embed-into-start-log >/dev/null 2>&1 || true
    fi
    if [ "$_final_status" = "failed" ]; then
        _print_why_block "$_s2_log_tail"
    fi
    _print_start_summary "$_final_status" "$_final_summary" "$_start_kind"
    _print_artifact_review "$_final_status" "$_start_kind"
    echo
    if [ -f ../../scripts/menu2_todo_board.py ]; then
        echo "${BOLD}-- LIVE START TODO (post-action) --${RESET}"
        python3 ../../scripts/menu2_todo_board.py \
            --bot "$_menu2_bot" \
            --probe-host "${IBKR_HOST:-127.0.0.1}" \
            --probe-port "$(configured_ibkr_port)" \
            print 2>/dev/null || true
        echo
    fi
    echo "  ${DIM}start log: $_start_log${RESET}"
    pause
}
item_stop() {
    echo "${BOLD}=== Stop ALL bots (cron disabled + processes killed) ===${RESET}"
    bash scripts/bot_control.sh stop all
    pause
}

item_restart() {
    echo "${BOLD}=== Restart THIS BOT only (options-v1.2 swing) ===${RESET}"
    bash deploy/stop.sh
    echo
    bash deploy/start.sh
    pause
}

item_gateway_only() {
    echo "${BOLD}=== Launch IB Gateway only (no bot) ===${RESET}"
    launch_gateway_runtime
    pause
}

item_launch_tws_only() {
    echo "${BOLD}=== Launch TWS only (no bot, no IBC autopilot) ===${RESET}"
    launch_tws_runtime
    pause
}

item_status() {
    echo "${BOLD}=== Live status ===${RESET}"
    # uvicorn running?
    if ss -tlnp 2>/dev/null | grep -qE ':8082\b'; then
        echo "  ${OK}✓${RESET} dashboard on :8082"
    else
        echo "  ${FAIL}✗${RESET} dashboard not running"
    fi
    # Gateway port from .env
    local port
    port=$(grep -E '^IBKR_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    port=${port:-4002}
    if ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; then
        echo "  ${OK}✓${RESET} IB Gateway on :$port"
    else
        echo "  ${FAIL}✗${RESET} IB Gateway NOT on :$port"
    fi
    # Kill switch
    if [ -f state/KILL ]; then
        echo "  ${WARN}!${RESET} KILL switch is SET — bot will halt on next cycle"
    else
        echo "  ${OK}✓${RESET} KILL switch not set"
    fi
    echo
    echo "Open positions (BOT-TRACKED in state/positions.json):"
    if [ -f state/positions.json ]; then
        python - <<'PY' 2>/dev/null || echo "  (could not parse positions.json)"
import json, pathlib
d = json.loads(pathlib.Path("state/positions.json").read_text() or "{}")
if not d:
    print("  (none)")
else:
    for sym, p in d.items():
        print(f"  {sym:<6} shares={p.get('shares',0):>6} "
              f"entry={p.get('entry',0):.2f} stop={p.get('stop',0):.2f} "
              f"peak={p.get('peak',0):.2f} layer={p.get('layer','?')}")
PY
    else
        echo "  (no positions.json yet)"
    fi

    echo
    echo "Open positions (LIVE from IB Gateway):"
    activate_venv >/dev/null 2>&1
    python - <<'PY' 2>/dev/null || echo "  (could not query IB — Gateway down or connect error)"
import sys
sys.path.insert(0, ".")
from broker.ibkr_client import IBKRClient
import json, pathlib
b = IBKRClient()
b.connect(timeout=5.0)
try:
    ib_pos = {}
    for p in b.ib.positions():
        if int(p.position) != 0:
            ib_pos[p.contract.symbol] = {"shares": int(p.position), "avg_cost": float(p.avgCost or 0)}
    if not ib_pos:
        print("  (none)")
    else:
        for sym, v in ib_pos.items():
            try:
                px = b.market_price(sym)
            except Exception:
                px = 0.0
            pnl = (px - v["avg_cost"]) * v["shares"] if v["avg_cost"] > 0 and px > 0 else 0
            mark = "+" if pnl >= 0 else "-"
            print(f"  {sym:<6} shares={v['shares']:>6} avg_cost={v['avg_cost']:.2f} "
                  f"now={px:.2f} unreal={mark}${abs(pnl):,.2f}")

    # Divergence check
    p = pathlib.Path("state/positions.json")
    if p.exists():
        tracked = json.loads(p.read_text() or "{}")
    else:
        tracked = {}
    tracked_syms = set(tracked.keys())
    ib_syms = set(ib_pos.keys())
    only_tracked = tracked_syms - ib_syms
    only_ib = ib_syms - tracked_syms
    qty_diff = []
    for sym in tracked_syms & ib_syms:
        t = tracked[sym].get("shares", 0)
        i = ib_pos[sym]["shares"]
        if t != i:
            qty_diff.append((sym, t, i))
    if only_tracked or only_ib or qty_diff:
        print()
        print("  !! DIVERGENCE DETECTED — bot state and IB disagree:")
        for sym in only_tracked:
            print(f"     {sym}: tracked but NOT in IB (phantom in state.json)")
        for sym in only_ib:
            print(f"     {sym}: in IB but NOT tracked (untracked position)")
        for sym, t, i in qty_diff:
            print(f"     {sym}: tracked={t} IB={i}")
        print("     Use option 24 to drill into a specific symbol.")
    else:
        print()
        print("  [OK] state/positions.json matches IB")
finally:
    b.disconnect()
PY
    pause
}

item_run_cycle() {
    echo "${BOLD}=== Run one main.py cycle ===${RESET}"
    if is_live_env_mode; then
        echo "${FAIL}REFUSED — live main.py cycles must use menu 41 / tools/live_launcher.py.${RESET}"
        echo "${DIM}This menu path is diagnostic-safe only and never sets LIVE_LAUNCHER_ONE_CYCLE.${RESET}"
        pause
        return 1
    fi
    activate_venv || return
    python main.py
    pause
}

item_gate_live() {
    echo "${BOLD}=== Current gate state (fresh SPY data) ===${RESET}"
    activate_venv || return
    python - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
from brain.data_feed import fetch_ohlcv
from brain.gate import evaluate as gate_eval
from brain.hmm_classifier import RegimeClassifier
from config import HMM_STATES, REGIME_ALLOWED_FOR_ENTRY, MARKET_GATE_MODE

spy = fetch_ohlcv("SPY", 400)
try:
    clf = RegimeClassifier(n_states=HMM_STATES)
    clf.fit(spy)
    regime = clf.predict(spy)
except Exception:
    regime = "neutral"
decision = gate_eval(spy, hmm_regime=regime, mode=MARKET_GATE_MODE,
                     regime_allowed_for_entry=REGIME_ALLOWED_FOR_ENTRY)
for line in decision.explain():
    print(line)
PY
    pause
}

item_backtest() {
    echo "${BOLD}=== Run OPTIONS backtest (10-year primary window) ===${RESET}"
    activate_venv || return
    python scripts/backtest_options.py --years 10
    pause
}

item_6mo_report() {
    echo "${BOLD}=== 6-month P/L breakdown ===${RESET}"
    activate_venv || return
    if [ ! -f state/backtest_options.json ]; then
        echo "${WARN}No backtest results found. Running backtest first...${RESET}"
        python scripts/backtest_options.py --years 10
    fi
    python scripts/pl_six_month.py
    pause
}

item_trade_audit() {
    echo "${BOLD}=== Trade-execution audit (capture ratios, exit reasons) ===${RESET}"
    activate_venv || return
    if [ ! -f state/backtest.json ]; then
        echo "${WARN}No backtest results — run option 8 first${RESET}"
        pause; return
    fi
    python scripts/trade_analysis.py --top 10
    pause
}

item_weekly_gate() {
    echo "${BOLD}=== Weekly gate audit (DISAGREE flags) ===${RESET}"
    activate_venv || return
    if [ ! -f state/backtest.json ]; then
        echo "${WARN}No backtest results — run option 8 first${RESET}"
        pause; return
    fi
    python scripts/gate_weekly_report.py --only-flags
    echo
    echo "Full report written to state/gate_weekly.md"
    pause
}

item_gate_trace() {
    echo "${BOLD}=== Per-day gate decision trace ===${RESET}"
    activate_venv || return
    if [ ! -f state/backtest.json ]; then
        echo "${WARN}No backtest results — run option 8 first${RESET}"
        pause; return
    fi
    read -r -p "Enter ISO week (e.g. 2025-W15) or leave blank for last 10 days: " wk
    if [ -z "$wk" ]; then
        python scripts/gate_trace.py --last-n 10
    else
        python scripts/gate_trace.py --week "$wk"
    fi
    pause
}

item_tail_dash() {
    echo "${BOLD}=== Dashboard / uvicorn live log ===${RESET}"
    # Pre-flight — show dashboard status before tailing
    if ss -tlnp 2>/dev/null | grep -qE ':8082\b'; then
        PID=$(pgrep -f 'uvicorn dashboard' | head -1)
        echo "  ${OK}\u2713${RESET} dashboard is RUNNING on :8082 (PID $PID)"
    else
        echo "  ${FAIL}\u2717${RESET} dashboard is NOT RUNNING (no listener on :8082)"
        echo "     Start it from the menu: option 2"
        pause
        return
    fi
    if [ ! -f state/dashboard.log ]; then
        echo "  ${WARN}!${RESET} state/dashboard.log not found yet"
        pause
        return
    fi
    LINES=$(wc -l < state/dashboard.log 2>/dev/null || echo 0)
    echo "  log file: state/dashboard.log ($LINES lines so far)"
    echo
    echo "  ${BOLD}Press Ctrl+C to stop watching (dashboard keeps running)${RESET}"
    echo "  ${DIM}---- live tail begins below ----${RESET}"
    echo
    tail -f state/dashboard.log
}

item_tail_dash_http() {
    echo "${BOLD}=== Dashboard HTTP requests only (filtered live tail) ===${RESET}"
    if ! ss -tlnp 2>/dev/null | grep -qE ':8082\b'; then
        echo "  ${FAIL}\u2717${RESET} dashboard is NOT RUNNING. Start with option 2."
        pause; return
    fi
    if [ ! -f state/dashboard.log ]; then
        echo "  ${WARN}!${RESET} no log file yet"
        pause; return
    fi
    echo "  Filtering for GET/POST/HTTP lines. Ctrl+C to stop."
    echo
    tail -f state/dashboard.log | grep --line-buffered -E "GET|POST|HTTP"
}

item_tail_decisions() {
    echo "${BOLD}=== Decisions log (Ctrl+C to exit) ===${RESET}"
    tail -f state/decisions.jsonl 2>/dev/null || echo "no decisions.jsonl yet"
}

item_kill_toggle() {
    if [ -f state/KILL ]; then
        rm -f state/KILL
        echo "${OK}✓${RESET} KILL switch REMOVED — bot will trade again on next cycle"
    else
        touch state/KILL
        echo "${WARN}!${RESET} KILL switch SET — bot will halt on next cycle"
    fi
    pause
}

item_git_status() {
    echo "${BOLD}=== Git state ===${RESET}"
    git log --oneline -5 2>&1 | head -5
    echo
    git status --short 2>&1
    pause
}

item_today_trades() {
    echo "${BOLD}=== Today's IB fills (live from broker) ===${RESET}"
    activate_venv || return
    read -r -p "Date [press Enter for today, or YYYY-MM-DD]: " d
    if [[ ! "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        d=$(date +%Y-%m-%d)
    fi
    python scripts/today_trades.py --date "$d"
    pause
}

item_clear_phantom_options() {
    # Heredoc-free — calls a real script. Heredoc form previously left
    # stdin in a weird state that broke the next `pause` read prompt.
    echo "${BOLD}=== Reconcile state/positions.json vs broker ===${RESET}"
    echo "Drops any option entry not actually held at IB (phantom-position guard)."
    echo "Stock positions left untouched."
    activate_venv || return
    python scripts/clear_phantom_options.py
    pause
}

item_cover_short() {
    echo "${BOLD}=== COVER (buy back) short positions — STK + OPT (PAPER ONLY) ===${RESET}"
    echo "${WARN}Cleanup tool for accidental shorts (e.g. dashboard double-click).${RESET}"
    echo "${DIM}Long-only guards now block NEW shorts; this unwinds existing ones.${RESET}"
    activate_venv || return
    echo
    echo "Choose:"
    echo "  1) Cover ONE stock symbol (you pick)"
    echo "  2) Cover ALL short positions (stocks + options)"
    read -r -p "[2]: " c
    c=${c:-2}
    if [ "$c" = "1" ]; then
        read -r -p "Symbol: " sym
        [ -z "$sym" ] && { echo "no symbol"; pause; return; }
        read -r -p "Quantity (blank = full cover): " qty
        if [ -z "$qty" ]; then
            python scripts/cover_short.py --symbol "$sym"
        else
            python scripts/cover_short.py --symbol "$sym" --qty "$qty"
        fi
    else
        python scripts/cover_short.py --all
    fi
    pause
}

item_premarket_ready() {
    echo "${BOLD}=== PRE-MARKET KICKOFF (one-button) ===${RESET}"
    echo "Pulls + chmods + seeds macro regime + starts v1.2 + options-v1.2."
    echo

    echo "[1/4] Updating repo + fixing permissions across ALL bots..."
    git config core.fileMode false 2>/dev/null || true
    if ! git diff --quiet || ! git diff --cached --quiet; then
        git stash push -u -m "kickoff-$(date +%s)" 2>/dev/null || true
    fi
    git pull --ff-only 2>&1 | sed 's/^/  /'
    git stash list 2>/dev/null | grep -q "kickoff-" && git stash pop >/dev/null 2>&1 || true
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [ -f "$REPO_ROOT/bootstrap.sh" ]; then
        bash "$REPO_ROOT/bootstrap.sh" 2>&1 | sed 's/^/  /'
    else
        chmod 775 deploy/*.sh scripts/*.sh run.sh 2>/dev/null || true
    fi

    echo
    echo "[2/4] Refreshing macro regime (skip if cached <7d)..."
    if [ ! -f state/macro_regime.json ] || \
       [ "$(find state/macro_regime.json -mtime +7 2>/dev/null | wc -l)" -gt 0 ]; then
        local PY="venv/bin/python"
        [ ! -x "$PY" ] && [ -x "../v1.2/venv/bin/python" ] && PY="../v1.2/venv/bin/python"
        if [ -x "$PY" ]; then
            "$PY" scripts/refresh_macro_regime.py 2>&1 | sed 's/^/  /' || echo "  ${WARN}refresh failed, continuing${RESET}"
        else
            echo "  ${WARN}venv missing — skipping macro refresh${RESET}"
        fi
    else
        echo "  cached regime is fresh; skip"
    fi

    echo
    echo "[3/4] Starting v1.2 swing (port 8080)..."
    bash scripts/bot_control.sh start v12 2>&1 | sed 's/^/  /'

    echo
    echo "[4/4] Starting options-v1.2 (port 8082)..."
    bash scripts/bot_control.sh start options 2>&1 | sed 's/^/  /'

    echo
    echo "${BOLD}=== READY ===${RESET}"
    local IP
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "  v1.2 swing:   http://${IP:-localhost}:8080"
    echo "  options-v1.2: http://${IP:-localhost}:8082"
    echo "  Verify after first cycle (~09:30 ET): bash run.sh 25 (daily dump)"
    pause
}

_restart_dash_at() {
    # Restart a single bot's uvicorn dashboard so it picks up post-pull code.
    # 2026-05-11 incident: git pull updated both bots but operator only ran
    # this bot's deploy/start.sh, leaving the sibling dashboard stale.
    local dir="$1" port="$2" name="$3"
    if [ ! -d "$dir" ]; then
        echo "  ${WARN}!${RESET} $name dir missing at $dir — skip"
        return
    fi
    if ! ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; then
        echo "  ${WARN}!${RESET} $name dashboard not running on :$port — skip"
        return
    fi
    echo "  restarting $name dashboard on :$port..."
    local pid
    pid=$(ss -tlnp 2>/dev/null | grep ":${port}\b" | grep -oP 'pid=\K[0-9]+' | head -1 || true)
    if [ -n "${pid:-}" ]; then
        kill "$pid" 2>/dev/null && sleep 2
    fi
    ( cd "$dir" && nohup uvicorn dashboard.app:app --host 127.0.0.1 --port "$port" \
        >> state/dashboard.log 2>&1 & )
    sleep 3
    if ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; then
        echo "  ${OK}\u2713${RESET} $name dashboard back up on :$port"
    else
        echo "  ${FAIL}\u2717${RESET} $name dashboard FAILED to restart — see $dir/state/dashboard.log"
    fi
}

item_git_pull() {
    echo "${BOLD}=== Update repo (safe pull) ===${RESET}"
    local stash_msg="run.sh-autopull-$(date +%s)"
    local stashed=0
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "  local changes present — stashing as '$stash_msg'..."
        if git stash push -u -m "$stash_msg" >/dev/null 2>&1; then
            stashed=1
        fi
    fi
    echo "  pulling..."
    git pull --ff-only 2>&1 | sed 's/^/    /'
    local pull_rc=${PIPESTATUS[0]}
    if [ "$stashed" = "1" ]; then
        echo "  reapplying stash..."
        git stash pop >/dev/null 2>&1 || echo "    ${WARN}stash pop had conflicts — run 'git stash list' to recover${RESET}"
    fi
    if [ "$pull_rc" -eq 0 ]; then
        chmod 775 deploy/*.sh scripts/*.sh run.sh 2>/dev/null || true
        echo "  ${OK}\u2713${RESET} pulled, perms refreshed"
        echo
        echo "Recent commits:"
        git log --oneline -5 2>&1 | sed 's/^/  /'
        echo
        echo "${BOLD}Restarting dashboards (both bots picked up new code):${RESET}"
        _restart_dash_at "$(pwd)" 8082 "options-v1.2"
        _restart_dash_at "$(cd .. && pwd)/v1.2" 8080 "v1.2"
    else
        echo "  ${FAIL}\u2717${RESET} pull failed (rc=$pull_rc) — see output above"
    fi
    pause
}

item_exec_audit() {
    echo "${BOLD}=== Audit broker execution log ===${RESET}"
    activate_venv || return
    python scripts/exec_audit.py
    pause
}

item_trade_history() {
    echo "${BOLD}=== Recent IB fills + realized P&L (live from broker) ===${RESET}"
    echo "Pulls from IB Gateway directly. Shows what's actually moved your equity."
    activate_venv || return
    read -r -p "Days back [7]: " days
    days=${days:-7}
    python scripts/trade_history.py --days "$days"
    pause
}

item_daily_summary() {
    echo "${BOLD}=== Daily SMS summary (preview / send now) ===${RESET}"
    activate_venv || return
    echo "Choose:"
    echo "  1) Preview (dry-run, prints to screen)"
    echo "  2) Send now via smsbot"
    read -r -p "[1]: " c
    c=${c:-1}
    if [ "$c" = "2" ]; then
        python scripts/daily_summary.py
    else
        python scripts/daily_summary.py --dry-run
    fi
    pause
}

item_install_daily_cron() {
    # T-DAILY-SUMMARY-CRON-NOISE1 — daily_summary is REPORT-ONLY (no
    # broker calls, no orders, no menu 2 artifact). Cron output now
    # routes to state/daily_summary.log so it can't be mistaken for bot
    # runtime activity, and a crontab COMMENT line labels the entry
    # inline so `crontab -l` shows the role without inference.
    echo "${BOLD}=== Install daily SMS cron (16:05 ET, weekdays) ===${RESET}"
    local _CRON_COMMENT="# tradingbot daily_summary — REPORT-ONLY (no orders, no broker mutation; menu 22)"
    # T-DAILY-SUMMARY-CRON-WRAPPER-SCRIPT1 — the prior inline form
    # (T-DAILY-SUMMARY-CRON-QUOTE-FIX1) is preserved by the actual
    # script at ../../scripts/cron_daily_summary.sh which:
    #   - writes the banner to state/daily_summary.log
    #   - sources/finds the right venv (per-bot preferred, v1.2 fallback)
    #   - runs scripts/daily_summary.py with output appended to the log
    #   - records the exit code in the log
    #   - REPORT-ONLY: no broker path, no orders.
    # The cron entry now just shells the wrapper — short, testable, and
    # not vulnerable to shell-quoting drift across crontabs.
    LINE="5 16 * * 1-5  cd $(pwd) && bash ../../scripts/cron_daily_summary.sh"
    echo
    echo "Add these lines to your crontab (or run 'crontab -e' yourself):"
    echo
    echo "  $_CRON_COMMENT"
    echo "  $LINE"
    echo
    read -r -p "Install automatically? [y/N]: " yn
    if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
        # Filter out BOTH the prior comment line and the prior cron line
        # by widening grep from "daily_summary.py" to "daily_summary".
        (crontab -l 2>/dev/null | grep -v "daily_summary"; printf '%s\n%s\n' "$_CRON_COMMENT" "$LINE") | crontab -
        echo "${OK}installed.${RESET} Verify with: crontab -l"
        echo "${DIM}log: state/daily_summary.log (separate from state/cron.log)${RESET}"
    else
        echo "skipped — copy/paste manually if you want."
    fi
    pause
}

item_install_crontab() {
    echo "${BOLD}=== Install/refresh full crontab (options-v1.2 + stress-v1.0) ===${RESET}"
    # T-OPERATOR-FLOW-MENU2-DIAGNOSTIC-BACKBONE1 — print the canonical
    # sequence so the operator knows menu 48 is the precondition and
    # menu 2 is the post-step. install_crontab.sh itself already reads
    # the arming mode and refuses to uncomment main.py lines unless the
    # operator has set scheduled_cron via menu 48.
    echo "${DIM}Canonical sequence: menu 2 -> menu 48 -> menu 45 -> menu 28 -> menu 2${RESET}"
    echo "${DIM}Run this only AFTER menu 48 has set scheduled_cron; menu 2 is the bookend.${RESET}"
    bash scripts/install_crontab.sh
    echo
    echo "${BOLD}NEXT STEP — re-run menu 2 to capture the post-install canonical evidence.${RESET}"
    pause
}

item_buy_stock() {
    echo "${BOLD}=== BUY stock (manual via IB) ===${RESET}"
    activate_venv || return
    read -r -p "Symbol: " sym
    [ -z "$sym" ] && { echo "no symbol"; pause; return; }
    read -r -p "Shares: " sh
    [ -z "$sh" ] && { echo "no qty"; pause; return; }
    python scripts/buy_position.py --symbol "$sym" --shares "$sh"
    pause
}

item_buy_option() {
    echo "${BOLD}=== BUY option (manual via IB, MID = LMT at NBBO mid) ===${RESET}"
    echo "${DIM}MID = explicit LMT at (bid+ask)/2. IB's MIDPRICE algo is rejected${RESET}"
    echo "${DIM}on options/SMART (Error 387). Use --strike & --expiry for explicit;${RESET}"
    echo "${DIM}leave blank for auto-pick (~12.5% OTM, ~120 DTE call).${RESET}"
    activate_venv || return
    read -r -p "Symbol: " sym
    [ -z "$sym" ] && { echo "no symbol"; pause; return; }
    read -r -p "Contracts (each = 100 shares of underlying): " n
    [ -z "$n" ] && { echo "no qty"; pause; return; }
    read -r -p "Right (C/P) [C]: " right
    right=${right:-C}
    read -r -p "Strike (blank = auto-pick OTM): " strike
    read -r -p "Expiry YYYYMMDD (blank = auto-pick ~120 DTE): " expiry
    if [ -n "$strike" ] && [ -n "$expiry" ]; then
        python scripts/buy_option.py --symbol "$sym" --contracts "$n" --right "$right" --strike "$strike" --expiry "$expiry"
    else
        python scripts/buy_option.py --symbol "$sym" --contracts "$n" --right "$right"
    fi
    pause
}

item_buy_combo() {
    echo "${BOLD}=== BUY combo: 10 shares + 10 calls ===${RESET}"
    echo "${DIM}Long-stock + long-call paired entry. Both orders go to IB${RESET}"
    echo "${DIM}for the same symbol. Options use MID order type (LMT at mid).${RESET}"
    activate_venv || return
    read -r -p "Symbol: " sym
    [ -z "$sym" ] && { echo "no symbol"; pause; return; }
    read -r -p "Shares [10]: " sh
    sh=${sh:-10}
    read -r -p "Contracts [10]: " n
    n=${n:-10}
    echo
    echo "${WARN}Will place TWO orders:${RESET}"
    echo "  1) BUY $sh shares of $sym"
    echo "  2) BUY $n calls of $sym (auto-pick ~12.5% OTM, ~120 DTE)"
    read -r -p "Type YES to proceed: " confirm
    [ "$confirm" = "YES" ] || { echo cancelled; pause; return; }
    python scripts/buy_position.py --symbol "$sym" --shares "$sh" --force
    echo
    python scripts/buy_option.py --symbol "$sym" --contracts "$n" --right C --force
    pause
}

item_sell_position() {
    echo "${BOLD}=== SELL a STOCK position (emergency override via IB) ===${RESET}"
    echo "${WARN}INSTRUMENT BOUNDARY: this sells STOCKS, not option contracts.${RESET}"
    echo "${WARN}options-v1.2 normally does not open stocks. Only use this to${RESET}"
    echo "${WARN}unwind stray stock longs at IB. For options use menu item for${RESET}"
    echo "${WARN}sell_option.py. 2026-05-08 incident: --all swept 6 unrelated longs.${RESET}"
    activate_venv || return
    echo
    read -r -p "Type EMERGENCY to proceed, anything else to abort: " ack
    if [ "$ack" != "EMERGENCY" ]; then
        echo "aborted"
        pause
        return
    fi
    echo
    echo "Choose:"
    echo "  1) Sell ONE symbol (you pick)"
    echo "  2) Sell ALL open STOCK positions (paper, closes everything)"
    read -r -p "[1]: " c
    c=${c:-1}
    if [ "$c" = "1" ]; then
        read -r -p "Symbol: " sym
        [ -z "$sym" ] && { echo "no symbol"; pause; return; }
        read -r -p "Quantity (blank = all shares): " qty
        if [ -z "$qty" ]; then
            python scripts/sell_position.py --emergency --symbol "$sym"
        else
            python scripts/sell_position.py --emergency --symbol "$sym" --qty "$qty"
        fi
    else
        python scripts/sell_position.py --emergency --all
    fi
    pause
}

item_claim_position() {
    echo "${BOLD}=== Claim untracked IB position(s) into positions.json ===${RESET}"
    echo "When IB has a position the bot doesn't track, this adds it to"
    echo "positions.json so the bot manages stops + exits going forward."
    activate_venv || return
    echo
    echo "Choose:"
    echo "  1) Claim one symbol (you pick)"
    echo "  2) Claim ALL untracked positions"
    read -r -p "[1]: " c
    c=${c:-1}
    if [ "$c" = "1" ]; then
        read -r -p "Symbol: " sym
        [ -z "$sym" ] && { echo "no symbol"; pause; return; }
        python scripts/claim_position.py --symbol "$sym"
    else
        python scripts/claim_position.py --all-untracked
    fi
    pause
}

item_daily_dump() {
    echo "${BOLD}=== Daily full dump (everything in one paste-friendly file) ===${RESET}"
    echo "Compiles options-v1.2 + stress-v1.0 + IB fills + per-symbol audits."
    activate_venv || return
    read -r -p "Date [press Enter for today, or YYYY-MM-DD]: " d
    if [[ ! "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        d=$(date +%Y-%m-%d)
        echo "  using today: $d"
    fi
    python scripts/daily_dump.py --date "$d"
    echo
    echo "${BOLD}File: state/dump_${d}.txt${RESET}"
    echo "${DIM}cat state/dump_${d}.txt   # then copy/paste to share${RESET}"
    pause
}

item_trade_diagnose() {
    echo "${BOLD}=== Per-trade audit (the 'why did this trade happen' tool) ===${RESET}"
    activate_venv || return
    echo "Choose:"
    echo "  1) Audit one symbol (you pick)"
    echo "  2) Audit ALL open positions"
    echo "  3) Audit last 3 closed round-trips"
    read -r -p "[1]: " c
    c=${c:-1}
    case "$c" in
        1) read -r -p "Symbol: " sym
           [ -z "$sym" ] && { echo "no symbol"; pause; return; }
           python scripts/trade_diagnose.py --symbol "$sym"
           echo
           echo "${BOLD}Saved to:${RESET} state/audit_${sym^^}.txt"
           echo "${DIM}(copy/paste that file's contents to share)${RESET}" ;;
        2) python scripts/trade_diagnose.py --all-open ;;
        3) python scripts/trade_diagnose.py --recent 3 ;;
    esac
    pause
}

item_position_pnl() {
    echo "${BOLD}=== Per-position unrealized P&L (live from IB) ===${RESET}"
    echo "Shows which open positions are responsible for unrealized profit/loss."
    activate_venv || return
    python -c "
from broker.ibkr_client import IBKRClient
b = IBKRClient(); b.connect()
try:
    print(f'{\"sym\":<6} {\"shares\":>8} {\"avg_cost\":>10} {\"market_px\":>10} {\"unreal_pnl\":>12} {\"pct\":>7}')
    print('-' * 60)
    total = 0.0
    rows = []
    for p in b.ib.positions():
        sym = p.contract.symbol
        shares = int(p.position)
        if shares == 0: continue
        px = b.market_price(sym)
        cost = p.avgCost
        if cost > 0 and shares != 0:
            pnl = (px - cost) * shares
            pct = (px / cost - 1) * 100 if cost > 0 else 0
            rows.append((sym, shares, cost, px, pnl, pct))
            total += pnl
    rows.sort(key=lambda r: r[4])  # losers first
    for sym, shares, cost, px, pnl, pct in rows:
        marker = '+' if pnl >= 0 else '-'
        print(f'{sym:<6} {shares:>8} {cost:>10.2f} {px:>10.2f} {marker}\${abs(pnl):>10,.2f} {pct:>+6.1f}%')
    print('-' * 60)
    print(f'{\"TOTAL\":<6} {\"\":<8} {\"\":<10} {\"\":<10} {\"+\" if total>=0 else \"-\"}\${abs(total):>10,.2f}')
finally:
    b.disconnect()
"
    pause
}

item_set_dashboard_token() {
    # T-RUN-DASHTOKEN1 — guided DASHBOARD_MUTATION_TOKEN set + persist.
    #
    # Behavior:
    #   - hidden-input prompt for the token (read -s)
    #   - hidden-input re-entry; mismatch -> REFUSED, nothing saved
    #   - exports to current shell
    #   - persists to $HOME/.config/tradingbot/dashboard_token (chmod 600,
    #     parent dir chmod 700, raw bytes, no shell quoting). Outside
    #     git-tracked repo content.
    #   - reports next steps (restart dashboards + run menu 39 smoke test)
    #   - NEVER prints the token value
    echo "${BOLD}=== SET / UPDATE DASHBOARD TOKEN ===${RESET}"
    echo "Sets DASHBOARD_MUTATION_TOKEN for the current shell AND persists it"
    echo "to ${DIM}$DASHBOARD_TOKEN_FILE${RESET} (chmod 600)."
    echo
    echo "This token gates POST routes on both dashboards (/sell,"
    echo "/sell_option, /claim, /flatten, /purge_orphan, /rebuild_watchlist)."
    echo "${WARN}The value is never echoed back in plain text.${RESET}"
    echo

    local tok1="" tok2=""
    read -r -s -p "Enter token: " tok1; echo
    if [ -z "$tok1" ]; then
        echo "${FAIL}REFUSED — empty token; nothing saved${RESET}"
        tok1=""
        pause
        return
    fi
    case "$tok1" in
        *$'\n'*|*$'\r'*)
            echo "${FAIL}REFUSED — newline/CR in token; pick a single-line value${RESET}"
            tok1=""
            pause
            return
            ;;
    esac

    read -r -s -p "Re-enter token to confirm: " tok2; echo
    if [ "$tok1" != "$tok2" ]; then
        echo "${FAIL}REFUSED — entries do not match; nothing saved${RESET}"
        tok1=""; tok2=""
        pause
        return
    fi

    export DASHBOARD_MUTATION_TOKEN="$tok1"
    local shell_ok="YES"

    local dir
    dir="$(dirname "$DASHBOARD_TOKEN_FILE")"
    local persist_ok="NO"
    local persist_err=""
    if mkdir -p "$dir" 2>/dev/null; then
        chmod 700 "$dir" 2>/dev/null || true
        if ( umask 077 && printf '%s' "$tok1" > "$DASHBOARD_TOKEN_FILE" ); then
            chmod 600 "$DASHBOARD_TOKEN_FILE" 2>/dev/null || true
            persist_ok="YES"
        else
            persist_err="write failed: $DASHBOARD_TOKEN_FILE"
        fi
    else
        persist_err="mkdir failed: $dir"
    fi

    tok1=""; tok2=""

    echo
    echo "${BOLD}── result ──${RESET}"
    if [ "$shell_ok" = "YES" ]; then
        echo "  ${OK}✓${RESET} token set for current shell (value hidden)"
    fi
    if [ "$persist_ok" = "YES" ]; then
        echo "  ${OK}✓${RESET} persisted to $DASHBOARD_TOKEN_FILE (chmod 600)"
    else
        echo "  ${FAIL}✗${RESET} persistence failed: ${persist_err:-unknown}"
        echo "  ${DIM}token is active for THIS shell only; will not survive a restart${RESET}"
    fi

    echo
    echo "${BOLD}── next steps ──${RESET}"
    echo "  1. Restart dashboards so they pick up the new token:"
    echo "     - menu 18  (Restart THIS BOT only) for the current bot"
    echo "     - repeat from the other version's run.sh for the other bot"
    echo "  2. Verify auth wired correctly:"
    echo "     - menu 39  (DASHBOARD CONTROL / SMOKE TEST)"
    echo "       expect 'correct token: 400 OK' on both dashboards"
    echo
    echo "${DIM}Dashboards read DASHBOARD_MUTATION_TOKEN from their process env,${RESET}"
    echo "${DIM}so they MUST be restarted after a token change.${RESET}"
    pause
}

item_live_launcher_wizard() {
    # T-RUN-LIVEWIZARD1 — guided wrapper around tools/live_launcher.py.
    # Never bypasses the launcher's own confirm-phrase or fail-closed
    # gates. This is presentation, not logic.
    echo "${BOLD}=== LIVE LAUNCHER / DRY-RUN / ONE-CYCLE ===${RESET}"
    echo "${WARN}This menu invokes tools/live_launcher.py.${RESET}"
    echo "${WARN}LIVE mode places REAL orders on a REAL account.${RESET}"
    echo "${DIM}The launcher always re-confirms with an identity-bearing${RESET}"
    echo "${DIM}phrase before submitting; this wizard cannot bypass that.${RESET}"
    # T-OPERATOR-FLOW-MENU2-DIAGNOSTIC-BACKBONE1 — declare the canonical
    # sequence so the operator knows menu 2 is the bookend and menu 48
    # is the required precondition for any live one-cycle here.
    echo "${DIM}Canonical sequence: menu 2 -> menu 48 -> menu 45 -> menu 41 -> menu 2${RESET}"
    echo "${DIM}Run menu 2 BEFORE this wizard; menu 48 must already be set to supervised_one_cycle.${RESET}"
    echo

    echo "Mode:"
    echo "  1) paper  (safe; default)"
    echo "  2) live   (real broker order — requires phase + account-id)"
    read -r -p "Choose [1]: " _m
    _m="${_m:-1}"
    local mode
    case "$_m" in
        1) mode="paper" ;;
        2) mode="live" ;;
        *) echo "${FAIL}invalid mode${RESET}"; return 1 ;;
    esac

    echo
    echo "Bot:"
    echo "  1) v1.2          (stock bot)"
    echo "  2) options-v1.2  (options bot — DEFAULT for this menu)"
    read -r -p "Choose [2]: " _b
    _b="${_b:-2}"
    local bot
    case "$_b" in
        1) bot="v1.2" ;;
        2) bot="options-v1.2" ;;
        *) echo "${FAIL}invalid bot${RESET}"; return 1 ;;
    esac

    # T-RUN-LIVEWIZARD-LIVEPORT1: resolve account-id and live-port from
    # shell env -> bot .env -> refuse cleanly. Operator should not need
    # to remember shell exports. DASHBOARD_MUTATION_TOKEN presence is
    # checked here (value never printed).
    local phase="" account_id="" live_port="" extra_args=""
    if [ "$mode" = "live" ]; then
        echo
        echo "Rollout phase (mandatory for live):"
        echo "  1) phase1  (tight caps: MAX_POSITIONS=1, RISK_PER_TRADE=0.0125 — DEFAULT)"
        echo "  2) phase2  (config defaults; only when phase1 has been observed)"
        echo "  3) phase3  (config defaults; full rollout)"
        read -r -p "Choose [1]: " _p
        _p="${_p:-1}"
        case "$_p" in
            1) phase="phase1" ;;
            2) phase="phase2" ;;
            3) phase="phase3" ;;
            *) echo "${FAIL}invalid phase${RESET}"; return 1 ;;
        esac

        # Resolve account-id: shell env (IBKR_ACCOUNT_ID, then
        # LIVE_ACCOUNT_ID) -> bot-local .env (same two keys).
        account_id="${IBKR_ACCOUNT_ID:-}"
        [ -z "$account_id" ] && account_id="${LIVE_ACCOUNT_ID:-}"
        [ -z "$account_id" ] && account_id="$(read_env_key IBKR_ACCOUNT_ID)"
        [ -z "$account_id" ] && account_id="$(read_env_key LIVE_ACCOUNT_ID)"
        if [ -z "$account_id" ]; then
            echo "${FAIL}REFUSED — IBKR_ACCOUNT_ID missing.${RESET}"
            echo "${DIM}Add IBKR_ACCOUNT_ID=<your account, e.g. U12345> to .env${RESET}"
            echo "${DIM}or export it in this shell, then retry.${RESET}"
            return 1
        fi

        # Resolve live-port: IBKR_LIVE_PORT preferred, fall back to
        # IBKR_PORT. Live mode requires port in {4001, 7496}.
        live_port="${IBKR_LIVE_PORT:-}"
        [ -z "$live_port" ] && live_port="$(read_env_key IBKR_LIVE_PORT)"
        [ -z "$live_port" ] && live_port="${IBKR_PORT:-}"
        [ -z "$live_port" ] && live_port="$(read_env_key IBKR_PORT)"
        if [ -z "$live_port" ]; then
            echo "${FAIL}REFUSED — IBKR_LIVE_PORT / IBKR_PORT missing.${RESET}"
            echo "${DIM}Add IBKR_LIVE_PORT=7496 (live TWS) to .env, then retry.${RESET}"
            return 1
        fi
        case "$live_port" in
            4001|7496) ;;
            *)
                echo "${FAIL}REFUSED — live mode requires port 4001 or 7496; got '$live_port'.${RESET}"
                echo "${DIM}Set IBKR_LIVE_PORT=7496 (TWS) or 4001 (Gateway live) in .env.${RESET}"
                return 1
                ;;
        esac

        # DASHBOARD_MUTATION_TOKEN must be set (presence only — value
        # is never printed). Auto-loader at script start tries the
        # persisted file; .env is consulted as a final fallback.
        if [ -z "${DASHBOARD_MUTATION_TOKEN:-}" ]; then
            local tok_from_env
            tok_from_env="$(read_env_key DASHBOARD_MUTATION_TOKEN)"
            if [ -n "$tok_from_env" ]; then
                export DASHBOARD_MUTATION_TOKEN="$tok_from_env"
            fi
            tok_from_env=""
        fi
        if [ -z "${DASHBOARD_MUTATION_TOKEN:-}" ]; then
            echo "${FAIL}REFUSED — DASHBOARD_MUTATION_TOKEN missing.${RESET}"
            echo "${DIM}Use menu 42 (SET / UPDATE DASHBOARD TOKEN) or add it${RESET}"
            echo "${DIM}to .env, then retry. Token value is never printed.${RESET}"
            return 1
        fi

        extra_args="--phase $phase --account-id $account_id --live-port $live_port"
    fi

    echo
    # T-MENU2-SUPERVISED-LIVE-RUN-NO-DRYRUN-TRAP1 — spell out what each
    # answer actually does so the operator cannot accidentally accept
    # the preview-only default thinking it's a real cycle.
    if [ "$mode" = "live" ]; then
        echo "${WARN}Dry-run choice — read carefully:${RESET}"
        echo "${WARN}  y = PREVIEW ONLY — WILL NOT FIX STALE DATA OR RUN BOT${RESET}"
        echo "${DIM}      (writes state/live_launcher_preview.json; menu 2 still NEEDED)${RESET}"
        echo "${WARN}  N = REAL one-cycle (requires identity phrase below)${RESET}"
        echo "${DIM}      (writes state/live_launcher.json on rc=0; menu 2 row flips DONE)${RESET}"
    fi
    read -r -p "Dry-run? (y/N) [y]: " _d
    _d="${_d:-y}"
    local dry=""
    case "$_d" in
        y|Y|yes|YES) dry="--dry-run" ;;
        *)           dry="" ;;
    esac

    local one_cycle=""
    if [ -z "$dry" ] && [ "$mode" = "live" ]; then
        echo
        echo "${WARN}Non-dry LIVE invocation MUST be --one-cycle (no autonomous loop).${RESET}"
        read -r -p "Confirm --one-cycle? (y/N): " _oc
        case "$_oc" in
            y|Y|yes|YES) one_cycle="--one-cycle" ;;
            *)
                echo "${FAIL}aborted — non-dry live requires --one-cycle${RESET}"
                return 1
                ;;
        esac
    fi

    local cmd="python3 ../../tools/live_launcher.py --bot $bot --mode $mode $extra_args $dry $one_cycle"
    cmd="$(echo "$cmd" | tr -s ' ')"

    echo
    echo "${BOLD}=============================================="
    echo " COMMAND PREVIEW"
    echo "==============================================${RESET}"
    echo "  $cmd"
    echo
    if [ "$mode" = "live" ]; then
        echo "  ${FAIL}LIVE MODE${RESET}"
        echo "    bot:         $bot"
        echo "    phase:       $phase"
        echo "    account_id:  $account_id"
        echo "    live_port:   $live_port"
        echo "    token:       set (value hidden)"
        echo "    dry-run:     $([ -n "$dry" ] && echo yes || echo NO)"
        echo "    one-cycle:   $([ -n "$one_cycle" ] && echo yes || echo n/a)"
        if [ -n "$dry" ]; then
            echo
            echo "${WARN}DRY-RUN ONLY: this validates launcher wiring and preview env.${RESET}"
            echo "${WARN}It does NOT spawn main.py, does NOT write ib_positions/account_summary,${RESET}"
            echo "${WARN}and does NOT clear the stale-snapshot banner by itself.${RESET}"
            if [ "$phase" != "phase1" ]; then
                echo "${WARN}Phase $phase here is preview-only. First real supervised live cycle${RESET}"
                echo "${WARN}still requires explicit approval for any non-phase1 rollout.${RESET}"
            fi
        fi
        if [ -z "$dry" ]; then
            echo
            echo "${WARN}The launcher will ask you to type the identity-bearing${RESET}"
            echo "${WARN}phrase 'ENABLE LIVE TRADING ON ACCOUNT $account_id' to proceed.${RESET}"
            echo "${WARN}If you do not type that phrase exactly, the launcher refuses.${RESET}"
        fi
    else
        echo "  ${BOLD}PAPER MODE${RESET}"
        echo "    bot:         $bot"
        echo "    dry-run:     $([ -n "$dry" ] && echo yes || echo no)"
    fi
    echo

    read -r -p "Proceed with the command above? (y/N): " _go
    case "$_go" in
        y|Y|yes|YES) ;;
        *) echo "aborted"; return 0 ;;
    esac

    # Venv resolution with local-first then v1.2 fallback.
    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -d ../v1.2/venv ]; then
        echo "${DIM}local venv missing; using ../v1.2/venv${RESET}"
        # shellcheck disable=SC1091
        source ../v1.2/venv/bin/activate
    else
        echo "${FAIL}no venv found in ./venv or ../v1.2/venv — run setup first${RESET}"
        return 1
    fi

    echo
    echo "${BOLD}── handing off to tools/live_launcher.py ──${RESET}"
    eval "$cmd"
    local rc=$?
    echo
    echo "${BOLD}── launcher exit code: $rc ──${RESET}"
    if [ "$rc" -ne 0 ]; then
        echo "${WARN}Launcher returned non-zero. Common causes:${RESET}"
        echo "${DIM}  - refused phrase (rc=1)${RESET}"
        echo "${DIM}  - validation refusal (rc=3): missing token/account/port${RESET}"
        echo "${DIM}  - main.py path missing (rc=4)${RESET}"
        echo "${DIM}  - child subprocess error (rc=5)${RESET}"
        echo "${DIM}  - audit-write failure (rc=6) — never launched${RESET}"
    fi
    # T-MENU2-SUPERVISED-LIVE-RUN-NO-DRYRUN-TRAP1 — never let the
    # operator close menu 41 thinking a dry-run satisfied the supervised
    # one-cycle. Print a non-misleading status line and, when the just-
    # finished invocation was a dry-run in LIVE mode, offer to chain
    # into a REAL one-cycle from inside this wizard.
    if [ -n "$dry" ] && [ "$rc" -eq 0 ]; then
        echo
        echo "${WARN}PREVIEW ONLY completed — no live cycle ran.${RESET}"
        echo "${DIM}state/live_launcher_preview.json written; menu 2 TODO row${RESET}"
        echo "${DIM}stays NEEDED. account_summary / ib_positions NOT refreshed.${RESET}"
        if [ "$mode" = "live" ]; then
            echo
            read -r -p "Dry-run completed. Run REAL one-cycle now? This still requires the identity phrase. [y/N]: " _real
            case "$_real" in
                y|Y|yes|YES)
                    local real_cmd
                    real_cmd="$(echo "$cmd" | sed 's/ --dry-run//')"
                    if [ -z "$one_cycle" ]; then
                        real_cmd="$real_cmd --one-cycle"
                    fi
                    real_cmd="$(echo "$real_cmd" | tr -s ' ')"
                    echo
                    echo "${BOLD}── real-cycle command ──${RESET}"
                    echo "  $real_cmd"
                    echo "${WARN}The launcher will ask you to type the canonical phrase${RESET}"
                    echo "${WARN}'ENABLE LIVE TRADING ON ACCOUNT $account_id' to proceed.${RESET}"
                    echo "${BOLD}── handing off to tools/live_launcher.py (REAL) ──${RESET}"
                    eval "$real_cmd"
                    rc=$?
                    echo
                    echo "${BOLD}── launcher exit code (real): $rc ──${RESET}"
                    ;;
                *)
                    echo "${DIM}skipped — operator can re-enter menu 41 later${RESET}"
                    ;;
            esac
        fi
    fi
    # T-LIVE-CYCLE-EVIDENCE-BUNDLE1 — emit one compact evidence bundle
    # summarising the just-completed live one-cycle. Writes
    # state/live_cycle_evidence.json + prints a one-screen summary so
    # the operator (and Diagnostics) have a single paste-ready artefact
    # without grepping multiple state files. No broker calls, no orders.
    local _menu41_bot
    _menu41_bot="$(basename "$(pwd)")"
    case "$_menu41_bot" in
        v1.2|options-v1.2) ;;
        *) _menu41_bot="options-v1.2" ;;
    esac
    if [ -f ../../scripts/live_cycle_evidence.py ]; then
        echo
        echo "${BOLD}-- live cycle evidence bundle --${RESET}"
        python3 ../../scripts/live_cycle_evidence.py --bot "$_menu41_bot" 2>/dev/null \
            || echo "${WARN}live_cycle_evidence helper failed${RESET}"
    fi
    # T-OPERATOR-FLOW-MENU2-DIAGNOSTIC-BACKBONE1 — instruct the post-step
    # so Diagnostics has the canonical start.log.json evidence after this
    # launcher cycle. Without it the cycle is lower-trust per pipeline.md.
    echo
    echo "${BOLD}NEXT STEP — re-run menu 2 (Start menu) to write canonical start.log.json evidence.${RESET}"
    echo "${DIM}Diagnostics reads menu 2 evidence FIRST; without a fresh run, this launcher cycle is lower-trust.${RESET}"
    pause
}

item_guided_live_prep() {
    # T-RUN-LIVEPREP1 — guided live-prep orchestrator. Reuses the
    # existing menu actions 38/39 in order, then aggregates the
    # known-blocker matrix into one operator-facing summary.
    echo "${BOLD}=== GUIDED LIVE PREP / PRE-FLIGHT ===${RESET}"
    echo "Runs the existing readiness chain in order:"
    echo "  Stage 1/2:  Dashboard control / smoke test (menu 39 logic)"
    echo "  Stage 2/2:  Pre-live readiness check (menu 38 logic)"
    echo "${DIM}No broker calls. No orders placed. No state writes.${RESET}"
    echo "${DIM}Paper-safe — never flips live trading or stages credentials.${RESET}"
    echo

    local _orig_pause_def
    _orig_pause_def="$(declare -f pause)"
    pause() { :; }

    echo "${BOLD}╔════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}║   STAGE 1/2  Dashboard control / smoke    ║${RESET}"
    echo "${BOLD}╚════════════════════════════════════════════╝${RESET}"
    item_dashboard_control
    local _stage1_v12="${v12_summary:-UNKNOWN}"
    local _stage1_opt="${opt_summary:-UNKNOWN}"

    echo
    echo "${BOLD}╔════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}║   STAGE 2/2  Pre-live readiness check     ║${RESET}"
    echo "${BOLD}╚════════════════════════════════════════════╝${RESET}"
    item_pre_live_readiness
    local _stage2_summary="${summary:-UNKNOWN}"

    eval "$_orig_pause_def"

    local blockers=()
    local warnings=()

    if [ -z "${DASHBOARD_MUTATION_TOKEN:-}" ]; then
        blockers+=("DASHBOARD_MUTATION_TOKEN unset in shell")
    fi
    case "$_stage1_v12" in
        "READY")                  ;;
        "NOT RUNNING")            blockers+=("v1.2 dashboard not running on port 8080") ;;
        "PARTIAL "*)              warnings+=("v1.2 dashboard probe partial: $_stage1_v12") ;;
        "TOKEN MISMATCH")         blockers+=("v1.2 dashboard token MISMATCH (shell vs process env)") ;;
        "DASHBOARD ENV UNSET")    blockers+=("v1.2 dashboard env missing token — restart needed") ;;
        *)                        warnings+=("v1.2 dashboard probe inconclusive: $_stage1_v12") ;;
    esac
    case "$_stage1_opt" in
        "READY")                  ;;
        "NOT RUNNING")            blockers+=("options-v1.2 dashboard not running on port 8082") ;;
        "PARTIAL "*)              warnings+=("options-v1.2 dashboard probe partial: $_stage1_opt") ;;
        "TOKEN MISMATCH")         blockers+=("options-v1.2 dashboard token MISMATCH (shell vs process env)") ;;
        "DASHBOARD ENV UNSET")    blockers+=("options-v1.2 dashboard env missing token — restart needed") ;;
        *)                        warnings+=("options-v1.2 dashboard probe inconclusive: $_stage1_opt") ;;
    esac
    case "$_stage2_summary" in
        "READY")                  ;;
        "READY AFTER FIXES")      warnings+=("release_gate --strict refused (warnings present)") ;;
        "NOT READY")              blockers+=("release_gate / validator / sidecar refresh failed (see Stage 2 above)") ;;
        *)                        warnings+=("readiness summary inconclusive: $_stage2_summary") ;;
    esac

    local final="" final_color=""
    if [ "${#blockers[@]}" -eq 0 ] && [ "${#warnings[@]}" -eq 0 ]; then
        final="READY FOR NEXT LIVE STAGE"; final_color="$BOLD"
    elif [ "${#blockers[@]}" -eq 0 ]; then
        final="READY AFTER FIXES"; final_color="$WARN"
    else
        final="NOT READY"; final_color="$FAIL"
    fi

    echo
    echo "${BOLD}═══════════════════════════════════════════════"
    echo " GUIDED LIVE PREP — FINAL SUMMARY"
    echo "═══════════════════════════════════════════════${RESET}"
    echo "  Stage 1: v1.2 dashboard         $_stage1_v12"
    echo "  Stage 1: options-v1.2 dashboard $_stage1_opt"
    echo "  Stage 2: readiness verdict      $_stage2_summary"
    echo
    if [ "${#blockers[@]}" -gt 0 ]; then
        echo "${FAIL}Blockers (${#blockers[@]}):${RESET}"
        local b
        for b in "${blockers[@]}"; do
            echo "  - $b"
        done
        echo
    fi
    if [ "${#warnings[@]}" -gt 0 ]; then
        echo "${WARN}Warnings (${#warnings[@]}):${RESET}"
        local w
        for w in "${warnings[@]}"; do
            echo "  - $w"
        done
        echo
    fi
    echo "  ${final_color}OVERALL: $final${RESET}"
    echo
    echo "${DIM}Common pre-live blocker reminders:${RESET}"
    echo "${DIM}  - DASHBOARD_MUTATION_TOKEN must be set in the dashboard${RESET}"
    echo "${DIM}    PROCESS env (not just shell) — restart dashboards after${RESET}"
    echo "${DIM}    setting it.${RESET}"
    echo "${DIM}  - Resolve any TRGP-class orphan via menu Purge action or${RESET}"
    echo "${DIM}    scripts/claim_position.py --symbol X --purge-orphan.${RESET}"
    echo "${DIM}  - Sidecar staleness: re-run menu 37 if release_gate WARNs.${RESET}"
    echo "${DIM}  - Validator evidence: scripts/validate.sh writes${RESET}"
    echo "${DIM}    versions/options-v1.2/state/validator_last_run.json.${RESET}"
    echo
    echo "${DIM}Human approval is ALWAYS required before live deployment,${RESET}"
    echo "${DIM}even on a READY verdict.${RESET}"
    pause
}

item_dashboard_control() {
    # T-RUN-DASHCTL1 — guided dashboard status + token + auth smoke.
    # Read-only HTTP probes against both bot dashboards. Never edits
    # dashboard source. Never prints token values. Degrades cleanly
    # when a probe target is missing.
    echo "${BOLD}=== DASHBOARD CONTROL / SMOKE TEST ===${RESET}"
    echo "Probes both bot dashboards (8080 + 8082) over HTTP:"
    echo "  - port listening?"
    echo "  - /version commit hash"
    echo "  - token state (env presence + auth matrix)"
    echo "${DIM}No broker calls. No orders placed. No state writes.${RESET}"
    echo

    # T-RUN-LIVEPREP1-FIX1: declare globally so item_guided_live_prep
    # (menu 40) can read the per-dashboard verdicts after this function
    # returns. `local` here vanishes at function exit and forced the
    # wrapper to fall back to UNKNOWN.
    declare -g v12_summary="UNKNOWN"
    declare -g opt_summary="UNKNOWN"

    # shellcheck disable=SC2317
    probe_dashboard() {
        local name="$1" port="$2" summary_var="$3"
        local listening="NO" version="(no response)" auth_state="(skip)"
        echo "${BOLD}── $name (port $port) ──${RESET}"

        if ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; then
            listening="YES"
            echo "  port $port:        LISTENING"
        elif command -v lsof >/dev/null 2>&1 && \
             lsof -i :"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            listening="YES"
            echo "  port $port:        LISTENING (via lsof)"
        else
            echo "  port $port:        ${FAIL}NOT LISTENING${RESET}"
            echo "  ${DIM}dashboard not running on $port; skipping HTTP probes${RESET}"
            eval "$summary_var=\"NOT RUNNING\""
            return 0
        fi

        local v
        v="$(curl -fsS -m 3 "http://localhost:$port/version" 2>/dev/null)"
        if [ -n "$v" ]; then
            version="$v"
            echo "  /version:        $version"
        else
            echo "  /version:        ${WARN}no response${RESET}"
        fi

        local url="http://localhost:$port/rebuild_watchlist"
        local rc_no rc_wrong rc_right
        rc_no="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
                 -X POST "$url" 2>/dev/null || echo 000)"
        rc_wrong="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
                    -H 'X-Operator-Token: wrong-on-purpose' \
                    -X POST "$url" 2>/dev/null || echo 000)"
        case "$rc_no" in
            503) echo "  no token:        $rc_no  ${BOLD}OK${RESET} (dashboard token unset; fail-closed)" ;;
            401) echo "  no token:        $rc_no  ${BOLD}OK${RESET} (token configured; missing header)" ;;
            *)   echo "  no token:        $rc_no  ${FAIL}UNEXPECTED${RESET}" ;;
        esac
        case "$rc_wrong" in
            401) echo "  wrong token:     $rc_wrong  ${BOLD}OK${RESET} (constant-time mismatch refused)" ;;
            503) echo "  wrong token:     $rc_wrong  (token unset; cannot verify wrong-token branch)" ;;
            *)   echo "  wrong token:     $rc_wrong  ${FAIL}UNEXPECTED${RESET}" ;;
        esac

        if [ -n "${DASHBOARD_MUTATION_TOKEN:-}" ]; then
            rc_right="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
                        -H "X-Operator-Token: $DASHBOARD_MUTATION_TOKEN" \
                        -X POST "$url" 2>/dev/null || echo 000)"
            case "$rc_right" in
                400) echo "  correct token:   $rc_right  ${BOLD}OK${RESET} (auth passed; reached existing confirm gate)"
                     auth_state="OK" ;;
                200) echo "  correct token:   $rc_right  ${BOLD}OK${RESET} (auth passed; route returned 200)"
                     auth_state="OK" ;;
                401) echo "  correct token:   $rc_right  ${FAIL}MISMATCH${RESET} — shell token differs from dashboard env"
                     auth_state="MISMATCH" ;;
                503) echo "  correct token:   $rc_right  ${FAIL}DASHBOARD ENV UNSET${RESET} — dashboard process needs restart"
                     auth_state="DASHBOARD_UNSET" ;;
                *)   echo "  correct token:   $rc_right  ${WARN}check manually${RESET}"
                     auth_state="UNKNOWN" ;;
            esac
        else
            echo "  correct token:   ${WARN}SKIPPED${RESET} — DASHBOARD_MUTATION_TOKEN unset in shell"
            auth_state="SHELL_UNSET"
        fi

        if [ "$listening" = "YES" ] && \
           { [ "$rc_no" = "503" ] || [ "$rc_no" = "401" ]; } && \
           { [ "$rc_wrong" = "401" ] || [ "$rc_wrong" = "503" ]; }; then
            case "$auth_state" in
                OK)              eval "$summary_var=\"READY\"" ;;
                SHELL_UNSET)     eval "$summary_var=\"PARTIAL (token unset in shell)\"" ;;
                MISMATCH)        eval "$summary_var=\"TOKEN MISMATCH\"" ;;
                DASHBOARD_UNSET) eval "$summary_var=\"DASHBOARD ENV UNSET\"" ;;
                *)               eval "$summary_var=\"PARTIAL\"" ;;
            esac
        else
            eval "$summary_var=\"AUTH FAILED\""
        fi
    }

    probe_dashboard "v1.2"         8080 v12_summary
    echo
    probe_dashboard "options-v1.2" 8082 opt_summary

    echo
    echo "${BOLD}=============================================="
    echo " SUMMARY"
    echo "==============================================${RESET}"
    if [ -n "${DASHBOARD_MUTATION_TOKEN:-}" ]; then
        echo "  shell env: DASHBOARD_MUTATION_TOKEN  ${BOLD}SET${RESET} (value hidden)"
    else
        echo "  shell env: DASHBOARD_MUTATION_TOKEN  ${WARN}NOT SET${RESET}"
        echo "  ${DIM}(set it in the bot venv / systemd unit and restart dashboards)${RESET}"
    fi
    echo "  v1.2 dashboard:           $v12_summary"
    echo "  options-v1.2 dashboard:   $opt_summary"
    echo
    echo "${DIM}This is a status + smoke action. Restart a dashboard via${RESET}"
    echo "${DIM}existing menu option 18 (Restart THIS bot only) if needed.${RESET}"
    pause
}

item_pre_live_readiness() {
    # T-RUN-READINESS1 — guided readiness flow. Chains the three
    # existing tools (validate.sh, run_diagnostics_intent.py,
    # release_gate.py) in order, surfaces each step's pass/fail,
    # then classifies the overall result for the operator:
    #
    #   READY              everything green
    #   READY AFTER FIXES  blockers clear but warnings present
    #                       (release_gate --strict refused)
    #   NOT READY          a prerequisite failed OR release_gate
    #                       has explicit blockers
    #
    # Full tool output is preserved so the operator can audit
    # exactly which check failed. This is an orchestration wrapper
    # — none of the underlying engines are modified.
    echo "${BOLD}=== PRE-LIVE READINESS CHECK ===${RESET}"
    echo "Runs in order:"
    echo "  1. scripts/validate.sh        validator suite"
    echo "  2. tools/run_diagnostics_intent.py --yes   sidecar refresh"
    echo "  3. scripts/release_gate.py    aggregated verdict"
    echo "  4. scripts/release_gate.py --strict   warning-sensitive verdict"
    echo "${DIM}No broker calls. No orders placed. ~1-2 minutes.${RESET}"
    echo
    # Local-venv-first with v1.2 fallback (matches T-RUN-DIAGS1's
    # pattern; options-v1.2 may not always have its own venv).
    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -d ../v1.2/venv ]; then
        echo "${DIM}local venv missing; using ../v1.2/venv${RESET}"
        # shellcheck disable=SC1091
        source ../v1.2/venv/bin/activate
    else
        echo "${FAIL}no venv found in ./venv or ../v1.2/venv — run setup first${RESET}"
        return 1
    fi

    local rc_validate=0 rc_sidecar=0 rc_gate=0 rc_gate_strict=0

    echo "${BOLD}── 1/4 validator suite ──${RESET}"
    if [ -f ../../scripts/validate.sh ]; then
        # T-RUN-VALIDATEENV1: validate.sh assumes a venv with ruff /
        # mypy / pytest / bandit / pre-commit on PATH AND that CWD is
        # repo root. The per-bot venv often only holds runtime deps
        # (no lint/test tools), so try repo-root venv first, fall
        # back to per-bot venvs. Run inside a subshell so CWD / PATH
        # changes never leak back to the menu shell.
        (
            cd ../.. || exit 127
            validator_venv=""
            for candidate in venv versions/v1.2/venv versions/options-v1.2/venv; do
                if [ -f "$candidate/bin/activate" ] \
                   && [ -x "$candidate/bin/ruff" ]; then
                    validator_venv="$candidate"
                    break
                fi
            done
            if [ -z "$validator_venv" ]; then
                echo "${FAIL}NO USABLE VENV — ruff not installed in venv/, versions/v1.2/venv/, or versions/options-v1.2/venv/.${RESET}" >&2
                echo "${DIM}Bootstrap: source venv/bin/activate && pip install -r requirements.txt${RESET}" >&2
                exit 127
            fi
            echo "${DIM}validator venv: $validator_venv${RESET}"
            # shellcheck disable=SC1090
            source "$validator_venv/bin/activate"
            bash scripts/validate.sh
        ) || rc_validate=$?
    else
        echo "${FAIL}MISSING: ../../scripts/validate.sh${RESET}"
        rc_validate=127
    fi
    echo

    echo "${BOLD}── 2/4 diagnostic sidecar refresh ──${RESET}"
    if [ -f ../../tools/run_diagnostics_intent.py ]; then
        python3 ../../tools/run_diagnostics_intent.py --yes || rc_sidecar=$?
    else
        echo "${FAIL}MISSING: ../../tools/run_diagnostics_intent.py${RESET}"
        rc_sidecar=127
    fi
    echo

    echo "${BOLD}── 3/4 release_gate verdict (warnings tolerated) ──${RESET}"
    if [ -f ../../scripts/release_gate.py ]; then
        python3 ../../scripts/release_gate.py || rc_gate=$?
    else
        echo "${FAIL}MISSING: ../../scripts/release_gate.py${RESET}"
        rc_gate=127
    fi
    echo

    echo "${BOLD}── 4/4 release_gate --strict (warning-sensitive) ──${RESET}"
    if [ -f ../../scripts/release_gate.py ]; then
        python3 ../../scripts/release_gate.py --strict > /dev/null 2>&1 \
            || rc_gate_strict=$?
        if [ "$rc_gate_strict" -eq 0 ]; then
            echo "  strict verdict: PASS (no warnings)"
        else
            echo "  strict verdict: FAIL (warnings present; see verdict block above)"
        fi
    else
        rc_gate_strict=127
    fi
    echo

    # Classification
    # T-RUN-LIVEPREP1-FIX1: declare summary globally so menu 40's
    # wrapper can read the readiness verdict after this function
    # returns. summary_color stays local — only used inside this body.
    declare -g summary=""
    local summary_color="$RESET"
    if [ "$rc_validate" -ne 0 ] || [ "$rc_sidecar" -ne 0 ] || [ "$rc_gate" -ne 0 ]; then
        summary="NOT READY"
        summary_color="$FAIL"
    elif [ "$rc_gate_strict" -ne 0 ]; then
        summary="READY AFTER FIXES"
        summary_color="$WARN"
    else
        summary="READY"
        summary_color="$BOLD"
    fi

    echo "${BOLD}=============================================="
    echo " SUMMARY"
    echo "==============================================${RESET}"
    echo "  validator suite       exit=$rc_validate"
    echo "  sidecar refresh       exit=$rc_sidecar"
    echo "  release_gate          exit=$rc_gate"
    echo "  release_gate --strict exit=$rc_gate_strict"
    echo
    echo "  ${summary_color}OVERALL: $summary${RESET}"
    echo
    echo "${DIM}Human approval is ALWAYS required before live deployment,${RESET}"
    echo "${DIM}even on a READY verdict.${RESET}"
    pause
}

item_mark_diagnostic() {
    # T-STARTLOG-DIAGNOSTIC-MARK-MENU1 — operator wraps the
    # scripts/start_log_mark_diagnostic.py tool inside menu 46 so the
    # diagnostic-supersede flow is discoverable from the canonical menu
    # surface. The bot to mark is derived from the current run.sh's bot
    # dir (basename of cwd) — operators cannot accidentally mark the
    # OTHER bot without leaving menu 2's source-of-truth dir first.
    echo "${BOLD}=== MARK latest menu-2 start.log.json as diagnostic test ===${RESET}"
    local _bot
    _bot="$(basename "$(pwd)")"
    echo "  bot:     ${_bot}"
    echo "  target:  state/start.log.json"
    echo "  ${DIM}Adds diagnostic_test_superseded/at/reason to the latest start log.${RESET}"
    echo "  ${DIM}state/start.log.jsonl is NOT modified. No file deleted.${RESET}"
    echo
    local phrase
    read -r -p "Type 'MARK DIAGNOSTIC' to proceed: " phrase
    if [ "$phrase" != "MARK DIAGNOSTIC" ]; then
        echo "${WARN}phrase did not match — refused, no state change.${RESET}"
        pause
        return
    fi
    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -d ../v1.2/venv ]; then
        # shellcheck disable=SC1091
        source ../v1.2/venv/bin/activate
    fi
    python3 ../../scripts/start_log_mark_diagnostic.py \
        --bot "$_bot" \
        --reason "operator-marked via menu 46" \
        --confirm
    pause
}

item_refresh_server() {
    # T-RUN-REFRESH-SERVER1 + T-RUN-REFRESH-SERVER-DEBUG1 — single-action
    # post-pull server refresh with structured step-1 debug trace.
    # Restarts both dashboards at HEAD, refreshes diagnostics sidecars
    # (including dashboard smoke), prints release_gate, then emits a
    # one-line READY / NOT READY summary. Pure orchestration wrapper:
    # never edits tool logic, never touches broker/order paths.
    echo "${BOLD}=== REFRESH SERVER ===${RESET}"
    echo "Runs in order:"
    echo "  1. tools/dashboard_runtime.py restart              dashboard restart at HEAD"
    echo "  2. tools/run_diagnostics_intent.py --yes --best-effort   sidecar refresh"
    echo "  3. scripts/release_gate.py                         aggregated verdict"
    echo "  4. SUMMARY                                         READY / NOT READY recap"
    echo "${DIM}No broker calls. No orders placed. No cron mutation. ~30-90s.${RESET}"
    echo

    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -d ../v1.2/venv ]; then
        echo "${DIM}local venv missing; using ../v1.2/venv${RESET}"
        # shellcheck disable=SC1091
        source ../v1.2/venv/bin/activate
    else
        echo "${FAIL}no venv found in ./venv or ../v1.2/venv — run setup first${RESET}"
        return 1
    fi

    # T-RUN-REFRESH-SERVER-DEBUG1 — canonical debug log path. Both bot
    # wrappers write here so Diagnostics always finds the latest trace
    # at one well-known location.
    local dbg_log='../../versions/options-v1.2/state/refresh_server.log'
    mkdir -p "$(dirname "$dbg_log")" 2>/dev/null || true
    if ! : > "$dbg_log" 2>/dev/null; then
        dbg_log='/tmp/refresh_server.log'
        : > "$dbg_log" 2>/dev/null || true
    fi
    _rs_log() {
        # tee to terminal AND append to dbg_log; never fails the wrapper.
        local line="$*"
        printf '%s\n' "$line"
        printf '%s\n' "$line" >> "$dbg_log" 2>/dev/null || true
    }
    _rs_log "refresh_server debug log: $dbg_log"

    dashboard_probe_host() {
        local bind_json='../../versions/options-v1.2/state/dashboard_bind.json'
        local host
        host="$(
            python3 - "$bind_json" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
host = "127.0.0.1"
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    raw = str(data.get("bind_host") or "").strip()
    if raw and raw not in {"0.0.0.0", "::"}:
        host = raw
except Exception:
    pass
print(host)
PY
        )"
        printf '%s\n' "${host:-127.0.0.1}"
    }

    wait_dashboard_version() {
        # Emits one JSON trace line per attempt: {"port",N, "served":B|null,
        # "trace":[{"host":H,"status":S,"body":B}|{"host":H,"status":null,
        # "error":...}, ...]}. Returns 0 the first time served == expected,
        # 1 after 30s timeout. Output goes to the canonical dbg_log via
        # _rs_log, so Diagnostics can reconstruct the wait sequence.
        local port="$1" expected="$2" probe_host="$3" out_var="$4" tag="$5"
        local served="" deadline attempt=0 trace
        deadline=$((SECONDS + 30))
        while [ "$SECONDS" -lt "$deadline" ]; do
            attempt=$((attempt + 1))
            trace="$(
                python3 - "$probe_host" "$port" <<'PY'
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
hosts = []
for candidate in (host, "127.0.0.1", "localhost"):
    if candidate and candidate not in hosts:
        hosts.append(candidate)
events = []
served = None
for candidate in hosts:
    try:
        with urllib.request.urlopen(f"http://{candidate}:{port}/version", timeout=2) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            commit = None
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    raw = obj.get("commit") or obj.get("sha") or obj.get("head")
                    if raw:
                        commit = str(raw).strip()
            except (json.JSONDecodeError, ValueError):
                pass
            normalized = commit if commit else body
            events.append({"host": candidate, "status": resp.status, "body": body, "commit": commit})
            if resp.status == 200 and normalized:
                served = normalized
                break
    except Exception as exc:
        events.append({"host": candidate, "status": None, "error": f"{type(exc).__name__}: {exc}"})
print(json.dumps({"port": int(port), "served": served, "trace": events}))
PY
            )"
            _rs_log "  [${tag}] attempt=${attempt} ${trace}"
            served="$(printf '%s' "$trace" | python3 -c 'import json,sys
try:
    print(json.loads(sys.stdin.read()).get("served") or "")
except Exception:
    print("")' 2>/dev/null)"
            if [ -n "$served" ] && [ "$served" = "$expected" ]; then
                printf -v "$out_var" '%s' "$served"
                _rs_log "  [${tag}] MATCH expected=${expected} served=${served}"
                return 0
            fi
            sleep 1
        done
        return 1
    }

    local rc_restart=0 rc_sidecar=0 rc_gate=0
    local expected_head probe_host served_v12="" served_opt=""
    local tmp_json="" summary_output="" abort_reason=""
    expected_head="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    probe_host="$(dashboard_probe_host)"

    echo "${BOLD}── 1/4: dashboard runtime restart ──${RESET}"
    # T-DASHBOARD-RUNTIME-SELFHEAL1: capture pre-restart per-dashboard
    # state so the post-restart classification can distinguish a routine
    # restart (was up; recycled at HEAD) from a self-heal (was DOWN;
    # dashboard_runtime brought it back up). port_listening is a top-level
    # helper already defined in this run.sh; if it is missing we degrade
    # to "unknown" rather than break the menu.
    local _pre_v12_up="unknown" _pre_opt_up="unknown"
    if command -v port_listening >/dev/null 2>&1 \
        || type port_listening >/dev/null 2>&1; then
        if port_listening 8080; then _pre_v12_up="yes"; else _pre_v12_up="no"; fi
        if port_listening 8082; then _pre_opt_up="yes"; else _pre_opt_up="no"; fi
    fi
    _rs_log "pre_restart v1.2_listening=${_pre_v12_up} options_v1.2_listening=${_pre_opt_up}"
    echo "  pre-restart: v1.2 listening=${_pre_v12_up}  options-v1.2 listening=${_pre_opt_up}"

    if [ -f ../../tools/dashboard_runtime.py ]; then
        python3 ../../tools/dashboard_runtime.py restart || rc_restart=$?
    else
        echo "${FAIL}MISSING: ../../tools/dashboard_runtime.py${RESET}"
        rc_restart=127
    fi
    _rs_log "dashboard_runtime restart rc=${rc_restart}"
    _rs_log "expected_head=${expected_head}"
    _rs_log "probe_host=${probe_host}"

    if [ "$rc_restart" -ne 0 ]; then
        abort_reason="dashboard_runtime_restart_nonzero"
    elif ! wait_dashboard_version 8080 "$expected_head" "$probe_host" served_v12 v1.2; then
        abort_reason="v1.2_version_wait_timeout"
    elif ! wait_dashboard_version 8082 "$expected_head" "$probe_host" served_opt options_v1.2; then
        abort_reason="options_v1.2_version_wait_timeout"
    fi

    if [ -n "$abort_reason" ]; then
        _rs_log "abort_reason=${abort_reason}"
        echo
        echo "${FAIL}── ABORTED at step 1/4: ${abort_reason} ──${RESET}"
        echo "  refresh_server debug log: $dbg_log"
        echo "  see versions/v1.2/state/dashboard.log for details"
        echo "  see versions/options-v1.2/state/dashboard.log for details"
        pause
        return 1
    fi
    # T-DASHBOARD-RUNTIME-SELFHEAL1: classify each transition.
    #   pre=yes -> restarted              (was up, recycled at HEAD)
    #   pre=no  -> self_healed_from_down  (was down, came back up)
    #   pre=unknown -> ready              (port_listening helper absent)
    local _v12_action _opt_action
    case "$_pre_v12_up" in
        yes) _v12_action="restarted" ;;
        no)  _v12_action="self_healed_from_down" ;;
        *)   _v12_action="ready" ;;
    esac
    case "$_pre_opt_up" in
        yes) _opt_action="restarted" ;;
        no)  _opt_action="self_healed_from_down" ;;
        *)   _opt_action="ready" ;;
    esac
    _rs_log "dashboard_transition v1.2=${_v12_action} options_v1.2=${_opt_action}"
    _rs_log "step1_pass v1.2_served=${served_v12} options_v1.2_served=${served_opt} expected=${expected_head}"
    echo "  ${OK}✓${RESET} v1.2 dashboard up on :8080 (served $served_v12) [${_v12_action}]"
    echo "  ${OK}✓${RESET} options-v1.2 dashboard up on :8082 (served $served_opt) [${_opt_action}]"
    echo

    echo "${BOLD}── 2/4: sidecar refresh ──${RESET}"
    if [ -f ../../tools/run_diagnostics_intent.py ]; then
        python3 ../../tools/run_diagnostics_intent.py --yes --best-effort || rc_sidecar=$?
    else
        echo "${FAIL}MISSING: ../../tools/run_diagnostics_intent.py${RESET}"
        rc_sidecar=127
    fi
    echo

    echo "${BOLD}── 3/4: release_gate verdict ──${RESET}"
    tmp_json="$(mktemp /tmp/release_gate_refresh_server.XXXXXX.json 2>/dev/null || true)"
    if [ -f ../../scripts/release_gate.py ]; then
        if [ -n "$tmp_json" ]; then
            python3 ../../scripts/release_gate.py --json --json-path "$tmp_json" || rc_gate=$?
        else
            python3 ../../scripts/release_gate.py || rc_gate=$?
        fi
    else
        echo "${FAIL}MISSING: ../../scripts/release_gate.py${RESET}"
        rc_gate=127
    fi
    echo

    echo "${BOLD}── 4/4: SUMMARY ──${RESET}"
    if [ -n "$tmp_json" ] && [ -f "$tmp_json" ]; then
        summary_output="$(
            python3 - "$tmp_json" "$rc_sidecar" "$rc_gate" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)

rc_sidecar = int(sys.argv[2])
rc_gate = int(sys.argv[3])
verdict = data.get("verdict", {})
blockers = list(verdict.get("blockers") or [])
warnings = list(verdict.get("warnings") or [])
ready = bool(verdict.get("release_ready")) and rc_sidecar == 0 and rc_gate == 0

if ready:
    print("OVERALL: READY")
else:
    extras = []
    if rc_sidecar:
        extras.append(f"sidecar_refresh_exit={rc_sidecar}")
    if rc_gate:
        extras.append(f"release_gate_exit={rc_gate}")
    tail = f" ({', '.join(extras)})" if extras else ""
    print(f"OVERALL: NOT READY — {len(blockers)} blockers, {len(warnings)} warnings{tail}")

for item in blockers:
    print(f"BLOCKER: {item}")
for item in warnings:
    print(f"WARNING: {item}")
PY
        )"
        printf '%s\n' "$summary_output" | sed 's/^/  /'
        rm -f "$tmp_json"
    else
        if [ "$rc_sidecar" -eq 0 ] && [ "$rc_gate" -eq 0 ]; then
            echo "  OVERALL: READY"
        else
            echo "  OVERALL: NOT READY — step failures present"
        fi
    fi

    # T-MENU45-ARTIFACT-SUMMARY1 — compact evidence pointer the operator
    # can paste alongside the OVERALL line. Files that exist now are
    # listed plainly; absent files are labeled "not yet observed" so a
    # paste never accuses the system of missing evidence that simply
    # hasn't been produced yet.
    _artifact_status() {
        # echoes the absolute-ish repo-rooted path, with " (not yet observed)"
        # suffix when the file is absent.
        local _p="$1"
        if [ -f "../../$_p" ]; then
            printf '%s' "$_p"
        else
            printf '%s (not yet observed)' "$_p"
        fi
    }
    echo
    echo "${DIM}-- evidence artifacts --${RESET}"
    echo "  refresh_server:    $(_artifact_status versions/options-v1.2/state/refresh_server.log)"
    echo "  release_gate:      $(_artifact_status versions/options-v1.2/state/release_gate.json)"
    echo "  dashboard_smoke:   $(_artifact_status versions/options-v1.2/state/dashboard_smoke.json)"
    echo "  start logs:"
    echo "    $(_artifact_status versions/v1.2/state/start.log.json)"
    echo "    $(_artifact_status versions/options-v1.2/state/start.log.json)"
    echo "  tws_exit_signal:"
    echo "    $(_artifact_status versions/v1.2/state/tws_exit_signal.json)"
    echo "    $(_artifact_status versions/options-v1.2/state/tws_exit_signal.json)"
    echo "  start_log_history:"
    echo "    $(_artifact_status versions/v1.2/state/start.log.jsonl)"
    echo "    $(_artifact_status versions/options-v1.2/state/start.log.jsonl)"
    echo "  start_log_marker_script:"
    echo "    $(_artifact_status scripts/start_log_mark_diagnostic.py)"
    echo "  ${DIM}paste this block with the OVERALL line for Diagnostics${RESET}"

    # T-MENU45-EVIDENCE-ARTIFACTS-JSON1 — mirror the same artifact list to
    # state/evidence_artifacts.json in BOTH bot dirs so Diagnostics can
    # consume a machine-readable manifest without parsing the terminal
    # block. Schema_version=1; written from the same data source as the
    # render above. Failure is silent — the terminal block is the
    # primary artifact.
    local _overall_label="NOT_READY"
    if [ "$rc_sidecar" -eq 0 ] && [ "$rc_gate" -eq 0 ]; then
        _overall_label="READY"
    fi
    python3 - "$_overall_label" <<'PY' || true
import json
import os
import sys
import datetime as _dt

overall = sys.argv[1]
generated_at = _dt.datetime.now(_dt.UTC).isoformat()
# Paths are written from the bot dir cwd. Both targets are reachable
# via ../<other>/state/ and state/ so we use absolute-from-repo style.
ROOT = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
artifacts = [
    ("refresh_server",    "versions/options-v1.2/state/refresh_server.log"),
    ("release_gate",      "versions/options-v1.2/state/release_gate.json"),
    ("dashboard_smoke",   "versions/options-v1.2/state/dashboard_smoke.json"),
    ("start_log_v1.2",    "versions/v1.2/state/start.log.json"),
    ("start_log_options-v1.2", "versions/options-v1.2/state/start.log.json"),
    ("tws_exit_signal_v1.2",  "versions/v1.2/state/tws_exit_signal.json"),
    ("tws_exit_signal_options-v1.2", "versions/options-v1.2/state/tws_exit_signal.json"),
    ("start_log_history_v1.2", "versions/v1.2/state/start.log.jsonl"),
    ("start_log_history_options-v1.2", "versions/options-v1.2/state/start.log.jsonl"),
    ("start_log_marker_script", "scripts/start_log_mark_diagnostic.py"),
]
items = []
for label, rel in artifacts:
    full = os.path.join(ROOT, rel)
    items.append({"label": label, "path": rel, "present": os.path.exists(full)})
# T-MENU45-EVIDENCE-ARTIFACTS-RELATIVE-PATHS1 — explicit metadata so a
# downstream consumer can verify the paths are portable without
# re-deriving them. paths above are ALREADY repo-relative POSIX
# strings; these flags just declare that contract.
generated_from_cwd = os.path.relpath(os.getcwd(), ROOT).replace(os.sep, "/")
payload = {
    "schema_version": 1,
    "generated_at": generated_at,
    "source": "menu45_refresh_server",
    "overall": overall,
    "repo_relative": True,
    "generated_from_cwd": generated_from_cwd,
    "artifacts": items,
}
for bot in ("v1.2", "options-v1.2"):
    target_dir = os.path.join(ROOT, "versions", bot, "state")
    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "evidence_artifacts.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass
PY
    pause
}

item_watchlist_rebuild() {
    # T-WATCHLIST-REBUILD-PREVIEW-MENU1 — preview-first recovery for
    # the case where menu 37 refresh leaves row 4 CRITICAL. Confirm
    # phrase REQUIRED before any write. Calls the existing per-bot
    # scripts/build_watchlist.py from cwd — no new logic, no broker
    # calls, no order paths.
    echo "${BOLD}=== WATCHLIST REBUILD PREVIEW / RECOVERY ===${RESET}"
    local _bot
    _bot="$(basename "$(pwd)")"
    echo "  bot: ${_bot}"
    echo
    echo "Files that would be regenerated:"
    echo "  state/watchlist.json"
    echo "  state/watchlist_audit.json"
    echo
    echo "${DIM}-- current state (read-only) --${RESET}"
    python3 - <<'PY' || true
import json
import os
daily_path = '../../versions/options-v1.2/state/daily_report.json'
wl_path    = '../../versions/options-v1.2/state/watchlist_audit.json'
sev = None
score = None
if os.path.exists(daily_path):
    try:
        d = json.load(open(daily_path, encoding='utf-8'))
        sev = d.get('anomaly_severity') or d.get('severity') or d.get('anomaly_band')
        score = d.get('anomaly_score') or d.get('score')
    except Exception:
        pass
tickers = None
src = None
if os.path.exists(wl_path):
    try:
        w = json.load(open(wl_path, encoding='utf-8'))
        if isinstance(w.get('tickers'), list):
            tickers = len(w['tickers'])
        else:
            tickers = w.get('ticker_count')
        src = w.get('source')
    except Exception:
        pass
print(f"  daily_report severity={sev or '(missing)'}  anomaly_score={score}")
print(f"  watchlist_audit tickers={tickers}  source={src or '(missing)'}")
PY
    echo
    echo "${DIM}No broker calls. No orders placed. Rebuilds the watchlist sidecars only.${RESET}"
    echo
    local phrase
    read -r -p "Type 'REBUILD WATCHLIST' to proceed (anything else aborts): " phrase
    if [ "$phrase" != "REBUILD WATCHLIST" ]; then
        echo "${WARN}phrase did not match — refused, no state change.${RESET}"
        pause
        return
    fi
    activate_venv || { pause; return; }
    if [ ! -f scripts/build_watchlist.py ]; then
        echo "${FAIL}MISSING: scripts/build_watchlist.py in ${_bot}${RESET}"
        pause
        return
    fi
    echo
    echo "${BOLD}-- before --${RESET}"
    python3 - <<'PY' || true
import json, os
p = '../../versions/options-v1.2/state/daily_report.json'
if os.path.exists(p):
    d = json.load(open(p, encoding='utf-8'))
    sev = d.get('anomaly_severity') or d.get('severity') or d.get('anomaly_band')
    print(f"  daily_report severity={sev}  score={d.get('anomaly_score') or d.get('score')}")
PY
    echo
    echo "${BOLD}-- rebuilding... --${RESET}"
    python3 scripts/build_watchlist.py
    local _rc=$?
    echo
    echo "${BOLD}-- after --${RESET}"
    python3 - "$_rc" <<'PY' || true
import json, os, sys
rc = sys.argv[1] if len(sys.argv) > 1 else "?"
print(f"  build_watchlist exit rc={rc}")
p = '../../versions/options-v1.2/state/daily_report.json'
if os.path.exists(p):
    d = json.load(open(p, encoding='utf-8'))
    sev = d.get('anomaly_severity') or d.get('severity') or d.get('anomaly_band')
    print(f"  daily_report severity={sev}  score={d.get('anomaly_score') or d.get('score')}")
wlp = '../../versions/options-v1.2/state/watchlist_audit.json'
if os.path.exists(wlp):
    w = json.load(open(wlp, encoding='utf-8'))
    tickers = len(w['tickers']) if isinstance(w.get('tickers'), list) else w.get('ticker_count')
    print(f"  watchlist_audit tickers={tickers}  ok={w.get('ok', True)}")
print("  next: re-run menu 38 PRE-LIVE READINESS CHECK to confirm row 4")
PY
    pause
}

item_live_arming_mode() {
    # T-LIVE-ARMING-CONFIRM-MENU1 — explicit operator flow for setting
    # the live-trading arming state. Writes state/live_arming_mode.json
    # ONLY after the canonical confirm phrase for the chosen mode is
    # typed exactly. Does NOT uncomment cron. Does NOT run menu 41.
    # Does NOT place orders. Reading the current state is always safe.
    echo "${BOLD}=== LIVE ARMING MODE / APPROVAL ===${RESET}"
    echo "${DIM}Sets state/live_arming_mode.json (the source of truth release_gate row 11 reads).${RESET}"
    echo "${DIM}This menu NEVER uncomments cron, NEVER runs menu 41, NEVER places orders.${RESET}"
    # T-OPERATOR-FLOW-MENU2-DIAGNOSTIC-BACKBONE1 — print the canonical
    # sequences so the operator sees menu 2 is the bookend for any
    # arming change. docs/pipeline.md is the long-form reference.
    echo "${DIM}Canonical sequences (menu 2 is the bookend that writes start.log.json evidence):${RESET}"
    echo "${DIM}  supervised one-cycle:  menu 2 -> menu 48 -> menu 45 -> menu 41 -> menu 2${RESET}"
    echo "${DIM}  scheduled live cron:   menu 2 -> menu 48 -> menu 45 -> menu 28 -> menu 2${RESET}"
    echo "${DIM}Run menu 2 BEFORE this menu so the pre-action runtime state is captured.${RESET}"
    echo
    local _bot
    _bot="$(basename "$(pwd)")"
    case "$_bot" in
        v1.2|options-v1.2) ;;
        *)
            echo "${FAIL}refusing: unrecognised bot dir '$_bot' — run from versions/v1.2 or versions/options-v1.2${RESET}"
            pause; return 1 ;;
    esac
    activate_venv || { pause; return 1; }
    echo "${BOLD}-- current arming state --${RESET}"
    python3 ../../scripts/live_arming_mode.py --bot "$_bot" read --json | sed 's/^/  /'
    echo
    echo "Choose action:"
    echo "  1) READ current arming state (no change)"
    echo "  2) APPROVE supervised one-cycle  (lifts first_real_one_cycle only)"
    echo "  3) APPROVE scheduled live cron   (lifts first_real_one_cycle + bot_cron_resume)"
    echo "  4) DISARM live trading           (returns both items to HELD)"
    echo "  0) Cancel (no change)"
    read -r -p "Choose [0]: " _c
    _c="${_c:-0}"
    local mode="" expected_phrase=""
    case "$_c" in
        0) echo "${DIM}no change${RESET}"; pause; return 0 ;;
        1) echo "${DIM}read-only — state already printed above${RESET}"; pause; return 0 ;;
        2) mode="supervised_one_cycle"; expected_phrase="APPROVE SUPERVISED LIVE ONE CYCLE" ;;
        3) mode="scheduled_cron";       expected_phrase="APPROVE SCHEDULED LIVE TRADING" ;;
        4) mode="disarmed";              expected_phrase="DISARM LIVE TRADING" ;;
        *) echo "${FAIL}invalid choice${RESET}"; pause; return 1 ;;
    esac
    echo
    echo "Bot:          $_bot"
    echo "Target mode:  $mode"
    echo "Required phrase (case-sensitive, exact match):"
    echo "    $expected_phrase"
    echo
    echo "${WARN}This writes to state/live_arming_mode.json. It does NOT uncomment cron,${RESET}"
    echo "${WARN}does NOT run menu 41, and does NOT place any orders.${RESET}"
    echo
    local phrase
    read -r -p "Type the confirm phrase to proceed (anything else aborts): " phrase
    if [ "$phrase" != "$expected_phrase" ]; then
        echo "${WARN}phrase did not match — refused, no state change.${RESET}"
        pause
        return 1
    fi
    python3 ../../scripts/live_arming_mode.py --bot "$_bot" set \
        --mode "$mode" --phrase "$expected_phrase" --confirm
    local _rc=$?
    if [ "$_rc" -ne 0 ]; then
        echo "${FAIL}arming write failed (rc=$_rc) — state unchanged.${RESET}"
        pause
        return 1
    fi
    echo
    echo "${OK}arming mode written.${RESET}  release_gate row 11 will reflect it on next run."
    echo "${DIM}reminder: cron is NOT touched by this menu. If mode=scheduled_cron, you${RESET}"
    echo "${DIM}must still verify crontab -l shows the live entries.${RESET}"
    # T-LIVE-ARMING-POST-MENU48-VERIFY1 — immediately verify the wiring
    # took effect by reading release_gate row 11 for this bot. This is
    # read-only and confirms ARMED items + their next-step hints, so
    # the operator does not have to leave menu 48 to discover whether
    # the write succeeded.
    echo
    echo "${BOLD}-- post-write verification (release_gate row 11) --${RESET}"
    python3 ../../scripts/live_arming_mode.py --bot "$_bot" verify
    # T-LIVE-ARMING-ACTUAL-ENABLEMENT-EVIDENCE1 — surface the closeout
    # status so the operator sees whether the system is actually ready
    # for the chosen path (e.g. one_cycle_completed vs
    # awaiting_menu41_execution vs awaiting_menu28_install). Read-only;
    # writes state/no_trade_closeout.json as part of the same read.
    echo
    echo "${BOLD}-- no-trade closeout status --${RESET}"
    python3 ../../scripts/no_trade_closeout.py --bot "$_bot" --print-only \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"  status:         {d.get('status')}\"); print(f\"  arming_mode:    {d.get('arming_mode')}\"); print(f\"  main_py_cron:   {d.get('main_py_cron_active')}\"); print(f\"  live_launcher:  {d.get('live_launcher_present')}\"); print(f\"  trade_evidence: {d.get('trade_evidence_present')}\")" \
        || echo "${WARN}closeout summary failed${RESET}"
    # T-OPERATOR-FLOW-MENU2-DIAGNOSTIC-BACKBONE1 — chain hint based on
    # the just-written mode so the operator knows the next concrete step
    # and that menu 2 is the canonical bookend after.
    if [ "$mode" = "supervised_one_cycle" ]; then
        echo "${DIM}next step (supervised): menu 45 (REFRESH SERVER) -> menu 41 (LIVE LAUNCHER) -> menu 2 (capture canonical evidence)${RESET}"
    elif [ "$mode" = "scheduled_cron" ]; then
        echo "${DIM}next step (scheduled):  menu 45 (REFRESH SERVER) -> menu 28 (INSTALL CRONTAB) -> menu 2 (capture canonical evidence)${RESET}"
    else
        echo "${DIM}next step (disarmed):   re-run menu 2 to capture the post-disarm canonical evidence${RESET}"
    fi
    pause
}

item_refresh_diags() {
    # T-RUN-DIAGS1 + T-MENU37-DATA-REFRESH-OUTCOME-SUMMARY1.
    echo "${BOLD}=== REFRESH diagnostic sidecars ===${RESET}"
    echo "Runs tools/run_diagnostics_intent.py --yes which writes the four"
    echo "canonical sidecars release_gate / dashboard cockpit read:"
    echo "  versions/options-v1.2/state/daily_report.json"
    echo "  versions/options-v1.2/state/watchlist_audit.json"
    echo "  versions/options-v1.2/state/timeline.json"
    echo "  versions/options-v1.2/state/portfolio_report.json"
    echo "${DIM}No broker calls. No orders placed. ~30-60s.${RESET}"
    echo
    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -d ../v1.2/venv ]; then
        echo "${DIM}local venv missing; using ../v1.2/venv${RESET}"
        # shellcheck disable=SC1091
        source ../v1.2/venv/bin/activate
    else
        echo "${FAIL}no venv found in ./venv or ../v1.2/venv — run setup first${RESET}"
        return 1
    fi

    local _before_sev=""
    _before_sev="$(python3 - <<'PY' 2>/dev/null
import json
import os
p = '../../versions/options-v1.2/state/daily_report.json'
try:
    d = json.load(open(p, encoding='utf-8'))
    print(d.get('anomaly_severity') or d.get('severity') or d.get('anomaly_band') or '')
except Exception:
    print('')
PY
)"

    python3 ../../tools/run_diagnostics_intent.py --yes

    python3 - "$_before_sev" <<'PY' || true
import json
import os
import sys

before = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
daily_path = '../../versions/options-v1.2/state/daily_report.json'
wl_path    = '../../versions/options-v1.2/state/watchlist_audit.json'

sev_now = None
score_now = None
src_daily = None
if os.path.exists(daily_path):
    try:
        d = json.load(open(daily_path, encoding='utf-8'))
        sev_now = d.get('anomaly_severity') or d.get('severity') or d.get('anomaly_band')
        score_now = d.get('anomaly_score') or d.get('score')
        src_daily = 'versions/options-v1.2/state/daily_report.json'
    except Exception:
        pass

wl_ok = None
wl_tickers = None
wl_src = None
if os.path.exists(wl_path):
    try:
        w = json.load(open(wl_path, encoding='utf-8'))
        wl_ok = bool(w.get('ok', True))
        if isinstance(w.get('tickers'), list):
            wl_tickers = len(w['tickers'])
        else:
            wl_tickers = w.get('ticker_count')
        wl_src = w.get('source') or 'versions/options-v1.2/state/watchlist_audit.json'
    except Exception:
        pass

sev_norm    = (sev_now or '').upper()
before_norm = (before  or '').upper()
if sev_norm == 'CRITICAL':
    outcome = 'still_critical'
elif before_norm == 'CRITICAL' and sev_norm in ('WARN', 'OK', ''):
    outcome = 'improved'
elif before_norm and sev_norm and before_norm == sev_norm:
    outcome = 'unchanged'
elif not before_norm and sev_norm:
    outcome = 'observed'
elif before_norm and not sev_norm:
    outcome = 'unchanged'
else:
    outcome = 'changed'

print()
print("-- data refresh outcome --")
print(f"  daily_report: severity={sev_now or '(missing)'}  anomaly_score={score_now}  source={src_daily or '(missing)'}")
print(f"  watchlist_audit: ok={wl_ok}  tickers={wl_tickers}  source={wl_src or '(missing)'}")
print(f"  outcome: {outcome}")
if outcome == 'still_critical':
    print("  next: rebuild watchlist sidecars via menu 47 (WATCHLIST REBUILD PREVIEW / RECOVERY)")
elif outcome == 'improved':
    print("  next: re-run menu 38 PRE-LIVE READINESS CHECK to confirm row 4 is clean")
PY
    pause
}

item_dashboard_runtime() {
    # T-DASH-RUNTIME-FIX1 — one-command dashboard orchestrator.
    # status / start / restart / stop / smoke for both v1.2 (:8080) and
    # options-v1.2 (:8082) without manual ss/kill/uvicorn dance.
    echo "${BOLD}=== Dashboard runtime ===${RESET}"
    echo "Choose:"
    echo "  1) status   probe both dashboards (read-only)"
    echo "  2) restart  stop + start both (port-scoped, refuses non-uvicorn)"
    echo "  3) start    start any dashboard not already running"
    echo "  4) stop     stop both (only kills uvicorn on :8080/:8082)"
    echo "  5) smoke    machine-readable verdict + sidecar"
    read -r -p "[1]: " c
    c=${c:-1}
    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [ -d ../v1.2/venv ]; then
        # shellcheck disable=SC1091
        source ../v1.2/venv/bin/activate
    else
        echo "${FAIL}no venv found in ./venv or ../v1.2/venv — run setup first${RESET}"
        return 1
    fi
    case "$c" in
        1) python3 ../../tools/dashboard_runtime.py status ;;
        2) python3 ../../tools/dashboard_runtime.py restart ;;
        3) python3 ../../tools/dashboard_runtime.py start ;;
        4) python3 ../../tools/dashboard_runtime.py stop ;;
        5) python3 ../../tools/dashboard_runtime.py smoke --json ;;
        *) echo "${WARN}invalid choice${RESET}" ;;
    esac
    pause
}

# ---- Menu renderer -----------------------------------------------------

show_menu() {
    clear
    echo "${BOLD}╔════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}║   tradingbot options-v1.2 — operator menu         ║${RESET}"
    echo "${BOLD}╚════════════════════════════════════════════╝${RESET}"
    echo
    # T-OPERATOR-FLOW-MENU2-DIAGNOSTIC-BACKBONE1 — visibly cluster the
    # live-arming sequence at the top of the menu so the operator
    # never wonders which item runs first. Items 2/45/48/41/28 are
    # still listed in their original sections below; this is a
    # bookend reference, not a moved-item block.
    echo "  ${BOLD}-- Live arming sequence (menu 2 is the canonical bookend) --${RESET}"
    echo "      ${DIM}supervised one-cycle:  2 -> 48 -> 45 -> 41 -> 2${RESET}"
    echo "      ${DIM}scheduled live cron:   2 -> 48 -> 45 -> 28 -> 2${RESET}"
    echo
    echo "  ${DIM}-- Bot operation --${RESET}"
    echo "   1) Setup / refresh install"
    echo "   2) Start menu (toggle individual bots — v1.2 / options-v1.2 / stress)"
    echo "      ${DIM}canonical logged start path — Diagnostics source of truth${RESET}"
    echo "      ${DIM}(writes state/start.log.json; review FIRST for any start question)${RESET}"
    echo "   3) Stop ALL bots (kills crons + processes)"
    echo "  18) Restart THIS bot only (options-v1.2 swing)"
    echo "  19) Launch IB Gateway only (no bot)"
    echo "   4) Status (running? positions? kill switch?)"
    echo "   5) Run ONE main.py cycle"
    echo "   6) Toggle KILL switch (halt trading)"
    echo
    echo "  ${DIM}-- Diagnostics --${RESET}"
    echo "   7) Live gate state (fresh SPY right now)"
    echo "   8) Run 5-year backtest"
    echo "   9) 6-month report vs SPY"
    echo "  10) Trade-execution audit"
    echo "  11) Weekly gate audit (DISAGREE flags)"
    echo "  12) Gate trace (drill a specific week)"
    echo "  37) REFRESH diagnostic sidecars (run_diagnostics_intent --yes)"
    echo "  ${BOLD}38) PRE-LIVE READINESS CHECK (validate + sidecars + release_gate)${RESET}"
    echo "  39) DASHBOARD CONTROL / SMOKE TEST (status + token + auth probes)"
    echo "  ${BOLD}40) GUIDED LIVE PREP / PRE-FLIGHT (chains 39 + 38 with final summary)${RESET}"
    echo "  ${BOLD}41) LIVE LAUNCHER / DRY-RUN / ONE-CYCLE (wraps tools/live_launcher.py)${RESET}"
    echo "  ${BOLD}42) SET / UPDATE DASHBOARD TOKEN (persisted, hidden input)${RESET}"
    echo "  43) DASHBOARD RUNTIME (status/start/restart/smoke — both bots)"
    echo "  45) REFRESH SERVER — restart dashboards + refresh + readiness"
    echo "  46) MARK latest menu-2 start log as diagnostic test evidence"
    echo "  47) WATCHLIST REBUILD PREVIEW / RECOVERY"
    echo "  ${BOLD}48) LIVE ARMING MODE / APPROVAL — set or read state/live_arming_mode.json${RESET}"
    echo "  44) Launch TWS only (no bot, no IBC autopilot)"
    echo
    echo "  ${DIM}-- Logs --${RESET}"
    echo "  13) Tail dashboard log (full output)"
    echo "  14) Tail decisions log"
    echo "  17) Tail dashboard log (HTTP requests only)"
    echo
    echo "  ${DIM}-- Git --${RESET}"
    echo "  15) Git status (recent commits + changes)"
    echo "  29) Update repo (safe pull — auto-stash + chmod)"
    echo "  30) Today's trades (live IB fills — fast)"
    echo "  ${BOLD}31) PRE-MARKET KICKOFF (pull + perms + macro + start both bots)${RESET}"
    echo "  35) Clear phantom option positions (drop state entries not held at broker)"
    echo "  36) COVER short position (BUY-back, STK + OPT) — PAPER ONLY"
    echo "  16) Audit broker exec log (find IB integration bugs)"
    echo "  20) Trade history + P&L (live from IB) — answers 'where did my money go'"
    echo "  21) Per-position unrealized P&L — which positions are dragging"
    echo "  22) Daily SMS summary (preview / send now)"
    echo "  23) Install daily SMS cron (16:05 ET, weekdays)"
    echo "  24) Per-trade audit (paste-friendly report for one symbol or all)"
    echo "  25) DAILY FULL DUMP (everything for one day, paste-friendly)"
    echo "  26) Claim untracked IB position into bot tracking"
    echo "  27) SELL a position (manual override) — places live IB SELL"
    echo "  28) Install/refresh full crontab (options-v1.2 + stress) — auto-detects paths"
    echo
    echo "  ${DIM}-- Manual orders (test live execution) --${RESET}"
    echo "  32) BUY stock (manual)"
    echo "  33) BUY option (MID / LMT at NBBO mid)"
    echo "  34) BUY combo: 10 shares + 10 calls (paired entry)"
    echo
    echo "   0) Quit"
    echo
}

run_item() {
    case "$1" in
        1)  item_setup ;;
        2)  item_start ;;
        3)  item_stop ;;
        4)  item_status ;;
        5)  item_run_cycle ;;
        6)  item_kill_toggle ;;
        7)  item_gate_live ;;
        8)  item_backtest ;;
        9)  item_6mo_report ;;
        10) item_trade_audit ;;
        11) item_weekly_gate ;;
        12) item_gate_trace ;;
        13) item_tail_dash ;;
        14) item_tail_decisions ;;
        15) item_git_status ;;
        16) item_exec_audit ;;
        17) item_tail_dash_http ;;
        18) item_restart ;;
        19) item_gateway_only ;;
        20) item_trade_history ;;
        21) item_position_pnl ;;
        22) item_daily_summary ;;
        23) item_install_daily_cron ;;
        24) item_trade_diagnose ;;
        25) item_daily_dump ;;
        26) item_claim_position ;;
        27) item_sell_position ;;
        28) item_install_crontab ;;
        29) item_git_pull ;;
        30) item_today_trades ;;
        31) item_premarket_ready ;;
        35) item_clear_phantom_options ;;
        36) item_cover_short ;;
        32) item_buy_stock ;;
        33) item_buy_option ;;
        34) item_buy_combo ;;
        37) item_refresh_diags ;;
        38) item_pre_live_readiness ;;
        39) item_dashboard_control ;;
        40) item_guided_live_prep ;;
        41) item_live_launcher_wizard ;;
        42) item_set_dashboard_token ;;
        43) item_dashboard_runtime ;;
        44) item_launch_tws_only ;;
        45) item_refresh_server ;;
        46) item_mark_diagnostic ;;
        47) item_watchlist_rebuild ;;
        48) item_live_arming_mode ;;
        0)  exit 0 ;;
        *)  echo "${WARN}invalid choice: $1${RESET}"; sleep 1 ;;
    esac
}

# ---- Main loop ---------------------------------------------------------

# Non-interactive mode: bash run.sh 8  (runs backtest)
if [ $# -gt 0 ]; then
    run_item "$1"
    exit $?
fi

while true; do
    show_menu
    read -r -p "Choose: " choice
    run_item "${choice:-}"
done
