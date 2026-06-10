#!/usr/bin/env bash
# Clean shutdown — kill THIS bot's main.py + dashboard only.
# Leaves other bots' processes and Gateway alone.
#
# Does NOT close existing positions on the paper account — it just stops the
# Python processes. Any open positions are managed by stops on Gateway's side.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DASH_PORT=8082

C_OK=$'\e[32m✓\e[0m'
C_WARN=$'\e[33m!\e[0m'

echo "=== tradingbot stop (options-v1.2 — :$DASH_PORT) ==="
echo

# 1. Let main.py finish its current cycle gracefully (if running)
echo "[1/3] Waiting for in-flight main.py cycle from this bot (max 10s)..."
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! pgrep -f "$REPO_DIR.*main.py" > /dev/null; then
        echo "  $C_OK no main.py running for this bot"
        break
    fi
    sleep 1
done

# 2. Force-kill stragglers — TARGETED to this bot only
echo
echo "[2/3] Killing processes (this bot only)..."
pkill -f "$REPO_DIR.*main.py" 2>/dev/null && echo "  killed main.py" || echo "  no main.py"

# Kill uvicorn ONLY on our port, not other bots' dashboards
PID_ON_PORT=$(ss -tlnp 2>/dev/null | grep ":${DASH_PORT}\b" 2>/dev/null | grep -oP 'pid=\K[0-9]+' 2>/dev/null | head -1 || true)
if [ -n "${PID_ON_PORT:-}" ]; then
    kill "$PID_ON_PORT" 2>/dev/null && echo "  killed uvicorn on :$DASH_PORT (PID $PID_ON_PORT)" || true
else
    echo "  no uvicorn on :$DASH_PORT"
fi

sleep 2

# 3. Verify
echo
echo "[3/3] Verifying..."
if pgrep -f "$REPO_DIR.*main.py" > /dev/null; then
    echo "  $C_WARN main.py still alive — force killing"
    pkill -9 -f "$REPO_DIR.*main.py" 2>/dev/null || true
    sleep 1
fi
PID_ON_PORT=$(ss -tlnp 2>/dev/null | grep ":${DASH_PORT}\b" 2>/dev/null | grep -oP 'pid=\K[0-9]+' 2>/dev/null | head -1 || true)
if [ -n "${PID_ON_PORT:-}" ]; then
    echo "  $C_WARN port $DASH_PORT still bound (PID $PID_ON_PORT) — force killing"
    kill -9 "$PID_ON_PORT" 2>/dev/null || true
    sleep 1
fi
ss -tlnp 2>/dev/null | grep -qE ":${DASH_PORT}\b" && echo "  $C_WARN port $DASH_PORT still bound — manual intervention" \
    || echo "  $C_OK port $DASH_PORT free"

echo
echo "=== STOPPED ==="
echo
echo "  Other bots (v1.2/options-v1.3/stress) are unaffected."
echo "  Gateway is still running and logged in."
echo "  To restart bot:          bash $(dirname "$0")/start.sh"
