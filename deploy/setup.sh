#!/usr/bin/env bash
# All-in-one server setup / refresh.
# Run this any time. It's idempotent — safe to re-run.
#
#   bash deploy/setup.sh
#
# What it does, in order:
#   1. Ensures venv exists, packages are installed
#   2. Renders IBC config from .env (if .env has creds)
#   3. Refreshes SP500 top-50 list (weekly, skipped if less than 5 days old)
#   4. Rebuilds watchlist (daily, FFTY + MAG7 + SP500 top-50)
#   5. Installs cron schedule (idempotent — won't duplicate entries)
#   6. Reports final state and what you need to do manually

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

C_OK=$'\e[32m✓\e[0m'
C_WARN=$'\e[33m!\e[0m'
C_FAIL=$'\e[31m✗\e[0m'

echo "=== tradingbot setup ==="
echo "repo: $REPO_DIR"
echo

# 1. venv
echo "[1/6] Python venv"
if [ ! -d venv ]; then
    echo "  $C_WARN venv missing — creating..."
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
echo "  $C_OK venv active ($(python --version))"

# Install/update deps if requirements.txt is newer than the marker
MARKER=".venv_deps_installed"
if [ ! -f "$MARKER" ] || [ requirements.txt -nt "$MARKER" ]; then
    echo "  installing deps..."
    pip install --quiet --upgrade pip
    pip install --quiet --prefer-binary -r requirements.txt
    touch "$MARKER"
    echo "  $C_OK deps installed"
else
    echo "  $C_OK deps fresh"
fi

# IBC config — operator hand-edits ~/ibc/config.ini directly from the IBC zip
# template. Render-from-.env flow was removed; see commit message of
# "remove unused render_ibc_config flow" for context.

# 3. SP500 top-50 (weekly)
echo
echo "[3/6] SP500 top-50"
NASDAQ_FILE="state/nasdaq100.json"
if [ ! -f "$NASDAQ_FILE" ]; then
    echo "  refreshing Nasdaq 100 universe..."
    python scripts/build_nasdaq100.py > /dev/null 2>&1 && \
        echo "  $C_OK Nasdaq 100 rebuilt" || \
        echo "  $C_FAIL Nasdaq 100 build failed"
fi

SP500_FILE="state/sp500_top50.json"
STALE=true
if [ -f "$SP500_FILE" ]; then
    AGE_DAYS=$(( ( $(date +%s) - $(stat -c %Y "$SP500_FILE" 2>/dev/null || echo 0) ) / 86400 ))
    if [ "$AGE_DAYS" -lt 5 ]; then
        echo "  $C_OK SP500 list is $AGE_DAYS day(s) old — fresh enough"
        STALE=false
    fi
fi
if [ "$STALE" = "true" ]; then
    echo "  refreshing SP500 top-50 (takes ~20s)..."
    python scripts/build_sp500_top50.py > /dev/null 2>&1 && \
        echo "  $C_OK SP500 top-50 rebuilt" || \
        echo "  $C_FAIL SP500 build failed (network? rate limit?)"
fi

# 4. Watchlist (daily)
echo
echo "[4/6] Watchlist"
python scripts/build_watchlist.py 2>&1 | tail -1 | sed 's/^/  /'

# 5. Cron
echo
echo "[5/6] Cron schedule"
CRON_TAG="# tradingbot-managed"
EXISTING=$(crontab -l 2>/dev/null || true)
if echo "$EXISTING" | grep -q "$CRON_TAG"; then
    echo "  $C_OK cron entries already installed"
else
    echo "  installing cron entries (15-min cycles + weekly watchlist refresh)..."
    (
        echo "$EXISTING"
        echo "$CRON_TAG"
        echo "TZ=America/New_York"
        echo "*/15 9-15 * * 1-5 cd $REPO_DIR && venv/bin/python main.py >> state/cron.log 2>&1"
        echo "0 16 * * 1-5      cd $REPO_DIR && venv/bin/python main.py >> state/cron.log 2>&1"
        echo "30 19 * * 0       cd $REPO_DIR && venv/bin/python scripts/build_nasdaq100.py >> state/cron.log 2>&1"
        echo "0 20 * * 0        cd $REPO_DIR && venv/bin/python scripts/build_sp500_top50.py >> state/cron.log 2>&1"
        echo "55 8 * * 1        cd $REPO_DIR && venv/bin/python scripts/build_watchlist.py >> state/cron.log 2>&1"
    ) | crontab -
    echo "  $C_OK cron installed"
fi

# 6. State check + next-step guidance
echo
echo "[6/6] State check"
[ -f state/watchlist.json ] && echo "  $C_OK watchlist.json" || echo "  $C_FAIL watchlist.json missing"
[ -f state/nasdaq100.json ] && echo "  $C_OK nasdaq100.json" || echo "  $C_WARN nasdaq100.json missing"
[ -f state/sp500_top50.json ] && echo "  $C_OK sp500_top50.json" || echo "  $C_WARN sp500_top50.json missing"
[ -f .env ] && echo "  $C_OK .env" || echo "  $C_FAIL .env missing"

# Is Gateway running?
if ss -tlnp 2>/dev/null | grep -qE ':(4002|7497|4001|7496)\b'; then
    PORT=$(ss -tlnp 2>/dev/null | grep -oE ':(4002|7497|4001|7496)\b' | head -1 | tr -d ':')
    echo "  $C_OK Gateway API listening on port $PORT"
    ENV_PORT=$(grep -E '^IBKR_PORT=' .env | cut -d= -f2 | tr -d '"' | tr -d "'")
    if [ "$PORT" != "$ENV_PORT" ]; then
        echo "  $C_WARN .env has IBKR_PORT=$ENV_PORT but Gateway is on $PORT"
        echo "     fix: sed -i 's/^IBKR_PORT=.*/IBKR_PORT=$PORT/' .env"
    fi
else
    echo "  $C_WARN Gateway not listening on any known port"
    echo "     start Gateway manually and log in, then re-run this script"
fi

# Is dashboard running?
if ss -tlnp 2>/dev/null | grep -qE ':8080\b'; then
    echo "  $C_OK dashboard listening on :8080"
else
    echo "  $C_WARN dashboard not running"
    echo "     start with: nohup uvicorn dashboard.app:app --host 0.0.0.0 --port 8080 > state/dashboard.log 2>&1 &"
fi

echo
echo "=== DONE ==="
echo
echo "If Gateway is logged in and API is listening:"
echo "  run one cycle: python main.py"
echo "  dashboard:     http://$(hostname -I | awk '{print $1}'):8080"
echo
echo "Cron runs main.py every 15 min during market hours automatically."
