#!/usr/bin/env bash
# Install/refresh the FULL crontab with correct paths.
# Replaces any existing tradingbot-related entries.
#
# Run from versions/v1.2 (or any sibling) — paths auto-detect.
#
#   bash scripts/install_crontab.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V12_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$V12_DIR/../.." && pwd)"
STRESS_DIR="$REPO_ROOT/versions/stress-v1.0"
OPTIONS_DIR="$REPO_ROOT/versions/options-v1.2"

BOLD=$'\e[1m'; OK=$'\e[32m'; WARN=$'\e[33m'; RESET=$'\e[0m'

# T-LIVE-ARMING-ACTION-WIRING1 — consult the per-bot live arming
# decision before deciding whether to emit each bot's main.py cron
# lines ACTIVE or prefixed with #TWS_PHASE_C (commented, present-but-
# inert). scripts/live_arming_mode.py is the single source of truth
# for the decision; this script is a reader/applier only and writes
# state/cron_armed.json so release_gate can detect mismatches.
ARMING_PY="$REPO_ROOT/scripts/live_arming_mode.py"
PYBIN="$V12_DIR/venv/bin/python"
if [ ! -x "$PYBIN" ]; then
    PYBIN="python3"
fi
# Cross-platform stripper: on Windows the python helper's stdout has
# trailing CR (\r\n); $() preserves the \r which would land inside cron
# lines as a literal control char. `tr -d '\r'` keeps it clean on both
# Linux (no-op) and any cross-platform dev path.
V12_MAIN_PREFIX="$("$PYBIN" "$ARMING_PY" --bot v1.2          decide --field prefix         | tr -d '\r')"
OPT_MAIN_PREFIX="$("$PYBIN" "$ARMING_PY" --bot options-v1.2  decide --field prefix         | tr -d '\r')"
V12_MODE="$(       "$PYBIN" "$ARMING_PY" --bot v1.2          decide --field mode           | tr -d '\r')"
OPT_MODE="$(       "$PYBIN" "$ARMING_PY" --bot options-v1.2  decide --field mode           | tr -d '\r')"
V12_MAIN_ACTIVE="$("$PYBIN" "$ARMING_PY" --bot v1.2          decide --field main_py_active | tr -d '\r')"
OPT_MAIN_ACTIVE="$("$PYBIN" "$ARMING_PY" --bot options-v1.2  decide --field main_py_active | tr -d '\r')"

echo "${BOLD}=== Tradingbot crontab installer ===${RESET}"
echo "v1.2:     $V12_DIR"
echo "stress:   $STRESS_DIR"
echo "options:  $OPTIONS_DIR"
echo "server:   $(date)"
echo "arming:   v1.2=$V12_MODE  options-v1.2=$OPT_MODE"
echo "main.py:  v1.2 active=$V12_MAIN_ACTIVE   options-v1.2 active=$OPT_MAIN_ACTIVE"
if [ "$V12_MAIN_ACTIVE" != "true" ]; then
    echo "${WARN}v1.2 main.py cron lines will be installed COMMENTED (#TWS_PHASE_C); approve scheduled_cron via menu 48 to activate.${RESET}"
fi
if [ "$OPT_MAIN_ACTIVE" != "true" ]; then
    echo "${WARN}options-v1.2 main.py cron lines will be installed COMMENTED (#TWS_PHASE_C); approve scheduled_cron via menu 48 to activate.${RESET}"
fi
echo

# Backup
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="/tmp/crontab.bak.$TS"
crontab -l > "$BACKUP" 2>/dev/null || echo "" > "$BACKUP"
echo "backup: $BACKUP"

# Build the new crontab
NEW=$(cat <<EOF
# tradingbot-managed (auto-installed $TS)
TZ=America/New_York

# v1.2 swing bot — main loop every 15 min during market hours
${V12_MAIN_PREFIX}*/15 9-15 * * 1-5 cd $V12_DIR && venv/bin/python main.py >> state/cron.log 2>&1

# v1.2 EOD reconcile at 16:00 ET
${V12_MAIN_PREFIX}0 16 * * 1-5      cd $V12_DIR && venv/bin/python main.py >> state/cron.log 2>&1

# v1.2 watchlist refresh
30 19 * * 0       cd $V12_DIR && venv/bin/python scripts/build_nasdaq100.py >> state/cron.log 2>&1
0 20 * * 0        cd $V12_DIR && venv/bin/python scripts/build_sp500_top50.py >> state/cron.log 2>&1
55 8 * * 1        cd $V12_DIR && venv/bin/python scripts/build_watchlist.py >> state/cron.log 2>&1

# v1.2 daily SMS summary
5 16 * * 1-5      cd $V12_DIR && venv/bin/python scripts/daily_summary.py >> state/cron.log 2>&1

# stress-v1.0 buy slots (4 staggered 15:40-15:55 ET)
40 15 * * 1-5     cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot buy_1 >> state/cron.log 2>&1"
45 15 * * 1-5     cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot buy_2 >> state/cron.log 2>&1"
50 15 * * 1-5     cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot buy_3 >> state/cron.log 2>&1"
55 15 * * 1-5     cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot buy_4 >> state/cron.log 2>&1"

# stress-v1.0 sell slots (4 staggered 09:35-09:50 ET, Tue-Fri only)
35 9 * * 2-5      cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot sell_1 >> state/cron.log 2>&1"
40 9 * * 2-5      cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot sell_2 >> state/cron.log 2>&1"
45 9 * * 2-5      cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot sell_3 >> state/cron.log 2>&1"
50 9 * * 2-5      cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/stress_run.py --slot sell_4 >> state/cron.log 2>&1"

# stress-v1.0 weekly watchlist rebuild
0 8 * * 1         cd $STRESS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/build_watchlist.py >> state/cron.log 2>&1"

# options-v1.2 — long-call options bot (uses v1.2 venv, IBKR_CLIENT_ID=6)
${OPT_MAIN_PREFIX}*/15 9-15 * * 1-5 cd $OPTIONS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python main.py >> state/cron.log 2>&1"
${OPT_MAIN_PREFIX}0 16 * * 1-5      cd $OPTIONS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python main.py >> state/cron.log 2>&1"
30 19 * * 0       cd $OPTIONS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/build_nasdaq100.py >> state/cron.log 2>&1"
0 20 * * 0        cd $OPTIONS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/build_sp500_top50.py >> state/cron.log 2>&1"
55 8 * * 1        cd $OPTIONS_DIR && bash -c "source $V12_DIR/venv/bin/activate && python scripts/build_watchlist.py >> state/cron.log 2>&1"

# Gateway watchdog — every 5 min, 24/7. Script self-skips 03:50-04:10 ET IBC
# daily-restart window. Catches overnight + weekend crashes the old 9-16
# schedule missed.
*/5 * * * *       cd $V12_DIR && bash scripts/gateway_watchdog.sh >> state/cron.log 2>&1

# Macro regime refresh — Monday 08:00 ET, drives risk-multiplier co-signal
0 8 * * 1         cd $V12_DIR && venv/bin/python scripts/refresh_macro_regime.py >> state/cron.log 2>&1

# Auto-launch Gateway 60s after Kali reboot
@reboot           sleep 60 && nohup \$HOME/ibgateway/ibgateway1 > /tmp/gw.boot.log 2>&1 &
EOF
)

echo "$NEW"
echo
echo "${WARN}This will REPLACE your entire crontab.${RESET}"
echo "${WARN}Backup saved to $BACKUP — 'crontab $BACKUP' to roll back.${RESET}"
read -r -p "Install? [y/N]: " yn
if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
    echo "$NEW" | crontab -
    echo
    echo "${OK}installed.${RESET} Verify:"
    crontab -l | tail -20
    # T-LIVE-ARMING-ACTION-WIRING1 — record what was actually installed so
    # release_gate can detect mismatches between operator intent and the
    # installed crontab. lines_active = 2 per bot when active, 0 otherwise.
    V12_LINES_ACTIVE=2
    OPT_LINES_ACTIVE=2
    [ "$V12_MAIN_ACTIVE" = "true" ] || V12_LINES_ACTIVE=0
    [ "$OPT_MAIN_ACTIVE" = "true" ] || OPT_LINES_ACTIVE=0
    "$PYBIN" "$ARMING_PY" --bot v1.2 cron-armed-write \
        --main-py-active "$V12_MAIN_ACTIVE" --lines-active "$V12_LINES_ACTIVE" \
        > /dev/null || echo "${WARN}cron_armed.json write failed for v1.2${RESET}"
    "$PYBIN" "$ARMING_PY" --bot options-v1.2 cron-armed-write \
        --main-py-active "$OPT_MAIN_ACTIVE" --lines-active "$OPT_LINES_ACTIVE" \
        > /dev/null || echo "${WARN}cron_armed.json write failed for options-v1.2${RESET}"
    echo "${OK}cron_armed.json updated for both bots.${RESET}"
else
    echo "skipped — no changes made"
fi
