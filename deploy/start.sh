#!/usr/bin/env bash
# Clean start — run after Gateway is logged in.
#   1. Kills any stale main.py / uvicorn
#   2. Starts dashboard in background (logs to state/dashboard.log)
#   3. Runs ONE main.py cycle to populate state
#   4. Reports what's running
#
# Run with:   bash ~/code/tradingbot/deploy/start.sh

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

C_OK=$'\e[32m✓\e[0m'
C_WARN=$'\e[33m!\e[0m'
C_FAIL=$'\e[31m✗\e[0m'

read_env_key() {
    local key="$1" envfile="${2:-.env}"
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

is_live_env_mode() {
    local mode="${IBKR_MODE:-}"
    [ -z "$mode" ] && mode="$(read_env_key IBKR_MODE .env)"
    [ -z "$mode" ] && mode="$(read_env_key IBKR_MODE ../v1.2/.env)"
    [ "$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')" = "live" ]
}

echo "=== tradingbot clean start ==="
echo

# 1. Kill any stale processes — TARGETED to this bot's port only
echo "[1/4] Killing stale processes (this bot only)..."
# Kill main.py running from THIS folder, not other bots' main.py
pkill -f "$REPO_DIR.*main.py" 2>/dev/null && echo "  killed lingering main.py" || echo "  no main.py running"
# Kill uvicorn ONLY on our port (8082), not other bots' dashboards
DASH_PORT=8082
# Wrap in || true so empty grep doesn't trip pipefail/set -e
PID_ON_PORT=$(ss -tlnp 2>/dev/null | grep ":${DASH_PORT}\b" 2>/dev/null | grep -oP 'pid=\K[0-9]+' 2>/dev/null | head -1 || true)
if [ -n "${PID_ON_PORT:-}" ]; then
    kill "$PID_ON_PORT" 2>/dev/null && echo "  killed uvicorn on :$DASH_PORT (PID $PID_ON_PORT)" || true
else
    echo "  no uvicorn on :$DASH_PORT"
fi
sleep 2

# 2. Activate venv (own venv first, fall back to v1.2's shared venv)
if [ -d venv ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
    echo "  $C_OK venv active (local)"
elif [ -d ../v1.2/venv ]; then
    # shellcheck disable=SC1091
    source ../v1.2/venv/bin/activate
    echo "  $C_OK venv active (shared from ../v1.2)"
else
    echo "  $C_FAIL no venv found locally OR in ../v1.2 — run deploy/setup.sh in v1.2 first"
    exit 1
fi

# 3. Check Gateway is listening; auto-launch if not
# Read IBKR_PORT from .env if present (or fall back to ../v1.2/.env), else default
PORT=""
for envfile in .env ../v1.2/.env; do
    if [ -f "$envfile" ]; then
        PORT=$(grep -E '^IBKR_PORT=' "$envfile" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"' | tr -d "'" | tr -d ' ' || true)
        [ -n "$PORT" ] && break
    fi
done
PORT=${PORT:-4002}

# Operator-configurable launcher path (export IBGATEWAY_LAUNCHER in .bashrc to override).
# Defaults to the user's ibgateway1 helper script.
IBGATEWAY_LAUNCHER="${IBGATEWAY_LAUNCHER:-$HOME/ibgateway/ibgateway1}"

if ! ss -tlnp 2>/dev/null | grep -qE ":${PORT}\b"; then
    echo "  $C_WARN Gateway not on port $PORT — attempting auto-launch"
    if [ -x "$IBGATEWAY_LAUNCHER" ]; then
        nohup "$IBGATEWAY_LAUNCHER" > state/ibgateway.log 2>&1 &
        echo "  launched $IBGATEWAY_LAUNCHER (logs: state/ibgateway.log)"
        echo "  waiting up to 90s for Gateway to accept API connections..."
        for i in $(seq 1 90); do
            if ss -tlnp 2>/dev/null | grep -qE ":${PORT}\b"; then
                echo "  $C_OK Gateway listening on port $PORT (took ${i}s)"
                break
            fi
            sleep 1
        done
        if ! ss -tlnp 2>/dev/null | grep -qE ":${PORT}\b"; then
            echo "  $C_FAIL Gateway did not come up within 90s"
            echo "     Check state/ibgateway.log for errors."
            echo "     If it asks for credentials, log in once manually,"
            echo "     then re-run this script."
            exit 1
        fi
    else
        echo "  $C_FAIL launcher not found or not executable: $IBGATEWAY_LAUNCHER"
        echo "     Set IBGATEWAY_LAUNCHER env var to your IB Gateway start script,"
        echo "     or start Gateway manually then re-run."
        exit 1
    fi
else
    echo "  $C_OK Gateway already listening on port $PORT"
fi

# 4. Start dashboard in background
echo
echo "[2/4] Starting dashboard..."
mkdir -p state
nohup uvicorn dashboard.app:app --host 0.0.0.0 --port 8082 > state/dashboard.log 2>&1 &
DASH_PID=$!
sleep 3
if ss -tlnp 2>/dev/null | grep -qE ':8082\b'; then
    echo "  $C_OK dashboard up on :8082 (PID $DASH_PID)"
    echo "     logs: tail -f state/dashboard.log"
else
    echo "  $C_FAIL dashboard failed to start — check state/dashboard.log"
    exit 1
fi

# 5. Run one cycle to populate state (paper only) / skip in live mode
echo
echo "[3/4] Running first cycle..."
if is_live_env_mode; then
    echo "  $C_WARN live mode detected — skipping direct main.py cycle"
    echo "     menu 41 / tools/live_launcher.py is the only valid live-cycle path"
    echo "     cron remains unchanged; this start prepares runtime + dashboard only"
else
    python main.py 2>&1 | tee -a state/cron.log | tail -5
fi

# 6. Report final state
echo
echo "[4/4] Running services"
ss -tlnp 2>/dev/null | grep -E "${PORT}|8082" | sed 's/^/  /'
echo
echo "=== READY ==="
echo
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "  Dashboard:  http://$IP:8082"
echo "  Dashboard logs: tail -f $REPO_DIR/state/dashboard.log"
echo "  Bot decisions:  tail -f $REPO_DIR/state/decisions.jsonl"
echo "  Halt trading:   touch $REPO_DIR/state/KILL"
echo "  Stop all:       bash $SCRIPT_DIR/stop.sh"
