#!/usr/bin/env bash
# Bootstrap the tradingbot on Ubuntu 22.04+.
# Idempotent — safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
VENV_DIR="$REPO_DIR/venv"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip \
    xvfb x11-utils xauth \
    unzip curl wget \
    cron

echo "==> Python venv at $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
pip install --quiet --prefer-binary -r "$REPO_DIR/requirements.txt"

echo "==> Ensuring state/ exists"
mkdir -p "$REPO_DIR/state"

echo "==> Done. Next: configure IBC, install IB Gateway, and enable systemd units."
