#!/usr/bin/env bash
# Master start/stop control for all bots.
#
# Usage:
#   bash scripts/bot_control.sh                 (interactive menu)
#   bash scripts/bot_control.sh start v12
#   bash scripts/bot_control.sh stop  options
#   bash scripts/bot_control.sh start all
#   bash scripts/bot_control.sh status
#
# Bot identifiers:
#   v12      — v1.2 swing bot
#   options  — options-v1.2 bot
#   stress   — stress-v1.0 generator
#   all      — all three

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V12_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$V12_DIR/../.." && pwd)"
OPTIONS_DIR="$REPO_ROOT/versions/options-v1.2"
OPTIONS3_DIR="$REPO_ROOT/versions/options-v1.3"
STRESS_DIR="$REPO_ROOT/versions/stress-v1.0"

BOLD=$'\e[1m'; OK=$'\e[32m'; WARN=$'\e[33m'; FAIL=$'\e[31m'; DIM=$'\e[2m'; RESET=$'\e[0m'

# ---- Env helpers -----------------------------------------------------------
read_env_key_from() {
    local key="$1" envfile="$2"
    [ -f "$envfile" ] || { printf ''; return; }
    local v
    v="$(grep -E "^${key}=" "$envfile" 2>/dev/null | tail -1 | cut -d= -f2-)"
    v="${v%$'\r'}"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    case "$v" in
        \"*\") v="${v%\"}"; v="${v#\"}" ;;
        \'*\') v="${v%\'}"; v="${v#\'}" ;;
    esac
    printf '%s' "$v"
}

is_live_mode_dir() {
    local dir="$1"
    local mode="${IBKR_MODE:-}"
    [ -z "$mode" ] && mode="$(read_env_key_from IBKR_MODE "$dir/.env")"
    [ "$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')" = "live" ]
}

# ---- Cron-line patterns (used to identify which lines belong to which bot) ----
PATTERN_V12="$V12_DIR && venv/bin/python main.py"
PATTERN_OPTIONS="$OPTIONS_DIR &&"
PATTERN_OPTIONS3="$OPTIONS3_DIR &&"
PATTERN_STRESS="$STRESS_DIR &&"

# ---- Cron toggle helpers ---------------------------------------------------
disable_lines() {
    local pattern="$1"
    crontab -l 2>/dev/null | sed -E "s|^([^#].*$pattern.*)|# DISABLED \1|" | crontab -
}
enable_lines() {
    local pattern="$1"
    crontab -l 2>/dev/null | sed -E "s|^# DISABLED (.*$pattern.*)|\1|" | crontab -
}

# ---- Process kill helpers --------------------------------------------------
kill_v12() {
    pkill -f "$V12_DIR.*main.py" 2>/dev/null && echo "  killed v1.2 main.py" || true
    pkill -f "uvicorn.*8080" 2>/dev/null && echo "  killed v1.2 dashboard (:8080)" || true
}
kill_options() {
    pkill -f "$OPTIONS_DIR.*main.py" 2>/dev/null && echo "  killed options-v1.2 main.py" || true
    pkill -f "uvicorn.*8082" 2>/dev/null && echo "  killed options-v1.2 dashboard (:8082)" || true
}
kill_options3() {
    pkill -f "$OPTIONS3_DIR.*main.py" 2>/dev/null && echo "  killed options-v1.3 main.py" || true
    pkill -f "uvicorn.*8083" 2>/dev/null && echo "  killed options-v1.3 dashboard (:8083)" || true
}
kill_stress() {
    pkill -f "stress_run.py" 2>/dev/null && echo "  killed stress runner" || true
}

# ---- Start helpers (enable cron + run start.sh) ----------------------------
start_v12() {
    echo "${BOLD}=== START v1.2 swing ===${RESET}"
    if is_live_mode_dir "$V12_DIR"; then
        echo "  ${WARN}live mode detected — cron left unchanged${RESET}"
        echo "  ${DIM}Live main.py cycles must use menu 41 / tools/live_launcher.py.${RESET}"
    else
        enable_lines "$PATTERN_V12"
    fi
    cd "$V12_DIR" && bash deploy/start.sh
}
start_options() {
    echo "${BOLD}=== START options-v1.2 ===${RESET}"
    if is_live_mode_dir "$OPTIONS_DIR"; then
        echo "  ${WARN}live mode detected — cron left unchanged${RESET}"
        echo "  ${DIM}Live main.py cycles must use menu 41 / tools/live_launcher.py.${RESET}"
    else
        enable_lines "$PATTERN_OPTIONS"
    fi
    cd "$OPTIONS_DIR" && bash deploy/start.sh
}
start_options3() {
    echo "${BOLD}=== START options-v1.3 (auto-roll, :8083) ===${RESET}"
    enable_lines "$PATTERN_OPTIONS3"
    cd "$OPTIONS3_DIR" && bash deploy/start.sh
}
start_stress() {
    echo "${BOLD}=== START stress-v1.0 (cron only — no daemon) ===${RESET}"
    enable_lines "$PATTERN_STRESS"
    echo "  ${OK}cron enabled${RESET}; next slot fires at next 15:40 / 09:35 ET"
}

# ---- Stop helpers ----------------------------------------------------------
stop_v12() {
    echo "${BOLD}=== STOP v1.2 swing ===${RESET}"
    disable_lines "$PATTERN_V12"
    kill_v12
    echo "  ${OK}cron disabled, processes killed${RESET}"
}
stop_options() {
    echo "${BOLD}=== STOP options-v1.2 ===${RESET}"
    disable_lines "$PATTERN_OPTIONS"
    kill_options
    echo "  ${OK}cron disabled, processes killed${RESET}"
}
stop_options3() {
    echo "${BOLD}=== STOP options-v1.3 ===${RESET}"
    disable_lines "$PATTERN_OPTIONS3"
    kill_options3
    echo "  ${OK}cron disabled, processes killed${RESET}"
}
stop_stress() {
    echo "${BOLD}=== STOP stress-v1.0 ===${RESET}"
    disable_lines "$PATTERN_STRESS"
    kill_stress
    echo "  ${OK}cron disabled, processes killed${RESET}"
}

# ---- Status ----------------------------------------------------------------
show_status() {
    echo "${BOLD}=== Bot status ===${RESET}"
    local cron
    cron=$(crontab -l 2>/dev/null)

    for label in "v1.2:$PATTERN_V12" "options-v1.2:$PATTERN_OPTIONS" "stress-v1.0:$PATTERN_STRESS"; do
        local name="${label%%:*}"
        local pat="${label#*:}"
        local enabled disabled
        enabled=$(echo "$cron" | grep -cE "^[^#].*$(echo "$pat" | sed 's/[][\.*^$()+?{|]/\\&/g')")
        disabled=$(echo "$cron" | grep -cE "^# DISABLED.*$(echo "$pat" | sed 's/[][\.*^$()+?{|]/\\&/g')")
        if [ "$enabled" -gt 0 ]; then
            echo "  ${OK}\u2713${RESET} ${name}: ENABLED ($enabled cron lines)"
        elif [ "$disabled" -gt 0 ]; then
            echo "  ${WARN}o${RESET} ${name}: DISABLED ($disabled cron lines, paused)"
        else
            echo "  ${FAIL}\u2717${RESET} ${name}: NOT INSTALLED"
        fi
    done
    echo
    echo "  Listening ports:"
    ss -tlnp 2>/dev/null | grep -E ':80(80|81|82)\b|:4002\b' | awk '{print "    " $4}' || true
}

restart_gateway() {
    echo "${BOLD}=== Restart IB Gateway ===${RESET}"
    local launcher="${IBGATEWAY_LAUNCHER:-$HOME/ibgateway/ibgateway1}"
    local port="${IBKR_PORT:-4002}"

    if [ ! -x "$launcher" ]; then
        echo "  ${FAIL}launcher not executable: $launcher${RESET}"
        return 1
    fi

    echo "  killing stale java/ibgateway/displaybanner/ibcstart processes..."
    pkill -f "java.*ibgateway" 2>/dev/null || true
    pkill -f "displaybanner" 2>/dev/null || true
    pkill -f "ibcstart" 2>/dev/null || true
    sleep 3

    echo "  launching $launcher ..."
    nohup "$launcher" > /tmp/gw.menu_restart.log 2>&1 &

    echo "  waiting up to 90s for port $port..."
    local i
    for i in $(seq 1 90); do
        if ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; then
            echo "  ${OK}\u2713${RESET} Gateway up on port $port (took ${i}s)"
            return 0
        fi
        sleep 1
    done
    echo "  ${FAIL}\u2717${RESET} Gateway did NOT come up within 90s — see /tmp/gw.menu_restart.log"
    return 1
}

# ---- Interactive menu ------------------------------------------------------
interactive_menu() {
    while true; do
        clear
        echo "${BOLD}╔═══════════════════════════════════════════════╗${RESET}"
        echo "${BOLD}║  Bot control — start/stop each bot           ║${RESET}"
        echo "${BOLD}╚═══════════════════════════════════════════════╝${RESET}"
        echo
        show_status
        echo
        echo "  ${DIM}-- START --${RESET}"
        echo "   1) Start v1.2 swing"
        echo "   2) Start options-v1.2"
        echo "   3) Start stress-v1.0"
        echo "   4) Start ALL three"
        echo
        echo "  ${DIM}-- STOP --${RESET}"
        echo "   5) Stop v1.2"
        echo "   6) Stop options-v1.2"
        echo "   7) Stop stress-v1.0"
        echo "   8) Stop ALL"
        echo
        echo "  ${DIM}-- GATEWAY --${RESET}"
        echo "   G) Restart IB Gateway (kill + relaunch IBC wrapper)"
        echo
        echo "   9) Refresh status"
        echo "   0) Back to main menu"
        echo
        read -r -p "Choose: " c
        case "${c:-}" in
            1) start_v12; read -r -p "Press Enter..." _ ;;
            2) start_options; read -r -p "Press Enter..." _ ;;
            3) start_stress; read -r -p "Press Enter..." _ ;;
            4) start_v12; start_options; start_stress; read -r -p "Press Enter..." _ ;;
            5) stop_v12; read -r -p "Press Enter..." _ ;;
            6) stop_options; read -r -p "Press Enter..." _ ;;
            7) stop_stress; read -r -p "Press Enter..." _ ;;
            8) stop_v12; stop_options; stop_stress; read -r -p "Press Enter..." _ ;;
            9) ;;
            G|g) restart_gateway; read -r -p "Press Enter..." _ ;;
            0) return ;;
            *) echo "${WARN}invalid${RESET}"; sleep 1 ;;
        esac
    done
}

# ---- Non-interactive mode (CLI args) ---------------------------------------
if [ $# -eq 0 ]; then
    interactive_menu
    exit 0
fi

case "$1" in
    start)
        case "${2:-}" in
            v12) start_v12 ;;
            options) start_options ;;
            options3) start_options3 ;;
            stress) start_stress ;;
            all) start_v12; start_options; start_stress ;;
            *) echo "usage: $0 start {v12|options|options3|stress|all}" ; exit 1 ;;
        esac ;;
    stop)
        case "${2:-}" in
            v12) stop_v12 ;;
            options) stop_options ;;
            options3) stop_options3 ;;
            stress) stop_stress ;;
            all) stop_v12; stop_options; stop_options3; stop_stress ;;
            *) echo "usage: $0 stop {v12|options|options3|stress|all}" ; exit 1 ;;
        esac ;;
    status) show_status ;;
    *) echo "usage: $0 {start|stop|status} [target]" ; exit 1 ;;
esac
