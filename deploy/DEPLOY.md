# Deployment — Ubuntu 192.168.1.99

Paper-first deployment. Flip to live only after 2+ weeks of stable paper runs.

---

## 1. Get the code on the server

### Option A — rsync from workstation
```bash
# from your Windows workstation (Git Bash / WSL)
rsync -avz --exclude venv --exclude state --exclude __pycache__ \
  /c/Users/z/Desktop/code/tradingbot/ user@192.168.1.99:~/tradingbot/
```

### Option B — git push/pull
```bash
# on workstation: push your branch
git push origin main

# on server
git clone <your-repo-url> ~/tradingbot
# or
cd ~/tradingbot && git pull
```

---

## 2. Install Python dependencies

```bash
ssh user@192.168.1.99
cd ~/tradingbot
bash deploy/install.sh
```

This installs: python3, venv, pip deps, Xvfb (for IB Gateway headless), cron.

---

## 3. Install IB Gateway + IBC

### Canonical install locations — keep these clean

```
~/Jts/ibgateway/<VERSION>/ibgateway     ← Gateway binary (default installer layout)
~/Jts/ibgateway/<VERSION>/jars/         ← Gateway jars
~/code/ibc/                             ← IBC scripts
~/code/ibc/config.ini                   ← rendered from .env
```

**Never** install Gateway to custom paths like `~/ibgateway`. The IBC default scripts assume `~/Jts/ibgateway/<VERSION>/`. Non-default paths require symlinks that are easy to break.

### Verify state before every reinstall or major change

```bash
VERSION=$(ls ~/Jts/ibgateway/ 2>/dev/null | head -1)
echo "Version: $VERSION"
[ -x ~/Jts/ibgateway/$VERSION/ibgateway ] && echo "Binary OK" || echo "BINARY MISSING"
[ -f ~/code/ibc/config.ini ] && echo "IBC config OK" || echo "IBC CONFIG MISSING"
grep -q "TWS_MAJOR_VRSN=$VERSION" ~/code/ibc/gatewaystart.sh && echo "IBC version matches" || echo "IBC VERSION MISMATCH"
grep -q "^TradingMode=paper" ~/code/ibc/config.ini && echo "Paper mode set" || echo "MODE MISSING/WRONG"
```

All five lines should print "OK"/"set"/"matches". If any print a warning, fix before starting Gateway.

### Clean install (when things get messy)

```bash
# stop everything
pkill -f ibgateway; pkill -f jts4launch; sleep 3

# nuke all Gateway artifacts
rm -rf ~/ibgateway ~/ibgateway1 ~/ibgateway.old ~/Jts/ibgateway

# reinstall with defaults (answer Enter to every prompt)
cd ~
./ibgateway-stable-standalone-linux-x64.sh

# update IBC to match new version
VERSION=$(ls ~/Jts/ibgateway/ | head -1)
sed -i "s|^TWS_MAJOR_VRSN=.*|TWS_MAJOR_VRSN=$VERSION|" ~/code/ibc/gatewaystart.sh
sed -i "s|^TWS_PATH=.*|TWS_PATH=/home/kali/Jts|" ~/code/ibc/gatewaystart.sh
```

### First-time install

IB Gateway is a Java app that normally wants a GUI. On Debian with a desktop, the GUI runs natively. On headless, use Xvfb + IBC for auto-login/auto-restart.

### IB Gateway (stable version)
```bash
cd ~
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh

# Run in a display-less mode - the installer still requires Xvfb
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x16 &
./ibgateway-stable-standalone-linux-x64.sh -q
# installs to ~/Jts/ibgateway/<version>/
```

### IBC Alpha
```bash
cd ~
mkdir ibc && cd ibc
LATEST=$(curl -s https://api.github.com/repos/IbcAlpha/IBC/releases/latest | grep browser_download_url | grep Linux | cut -d '"' -f 4)
wget "$LATEST" -O ibc.zip
unzip ibc.zip && rm ibc.zip
chmod +x *.sh

# Verify gatewaystart.sh points to the right gateway version
# Edit TWS_PATH / TWS_MAJOR_VRSN in gatewaystart.sh to match your install
nano gatewaystart.sh
```

---

## 4. Configure runtime + broker credentials (separate paths)

The bot's `.env` and the broker login are SEPARATE config surfaces. The
bot `.env` does NOT carry IB login credentials — those live with the
broker (IBC `~/ibc/config.ini` or typed into the TWS GUI at login time).
Earlier docs listed `IBKR_USERNAME` / `IBKR_PASSWORD` in `.env` (rendered
via `deploy/render_ibc_config.sh`); those keys were dead (runtime code
never read them) and have been pruned (T-ENV-DEAD-KEYS-PRUNE1).

```bash
cd ~/tradingbot
cp .env.example .env
chmod 600 .env
nano .env
# IBKR_MODE=paper and IBKR_PORT=7497 are the safe defaults.
# This file controls bot ↔ Gateway connection only; it does NOT
# carry IB login credentials.

# Edit ~/ibc/config.ini directly (one-time on each Kali box)
# Set IbLoginId / IbPassword / TradingMode=paper / OverrideTwsApiPort=4002
# Use `chmod 600 ~/ibc/config.ini` after editing.
```

Any time you rotate credentials or switch paper ↔ live, edit `.env` then re-run
`render_ibc_config.sh` and `sudo systemctl restart ibc-gateway`.

---

## 5. Install systemd units

```bash
# Replace 'USER' below with your actual login name
USER=$(whoami)

# dashboard
sed "s/%i/$USER/g" deploy/systemd/tradingbot-dashboard.service | \
  sudo tee /etc/systemd/system/tradingbot-dashboard.service > /dev/null

# IB Gateway via IBC
sed "s/%i/$USER/g" deploy/systemd/ibc-gateway.service | \
  sudo tee /etc/systemd/system/ibc-gateway.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now ibc-gateway.service
sudo systemctl enable --now tradingbot-dashboard.service

# verify
sudo systemctl status ibc-gateway --no-pager
sudo systemctl status tradingbot-dashboard --no-pager
```

Dashboard should now be reachable at `http://192.168.1.99:8080`.

---

## 6. Install cron jobs

```bash
# Edit the template to replace USER
sed "s/USER/$(whoami)/g" ~/tradingbot/deploy/crontab > /tmp/bot.cron
crontab /tmp/bot.cron
crontab -l    # verify
```

---

## 7. Build initial watchlist + smoke test

```bash
cd ~/tradingbot
source venv/bin/activate

# pull the FFTY 50 tickers
python scripts/build_watchlist.py

# run the rules-based scan (should show ~10-20 stocks passing trend template)
python scripts/scan.py --no-fundamentals

# TEST ONE CYCLE in paper mode (IB Gateway must be logged in)
python main.py
# → expected output: regime=... positions=... heat=...
```

If `main.py` errors with `Connection refused`, IB Gateway isn't up yet — wait 1–2 min after `systemctl start ibc-gateway`, then retry.

---

## 8. Verify end-to-end

- [ ] Dashboard reachable at `http://192.168.1.99:8080`
- [ ] `state/state.json` updated after running `main.py`
- [ ] SMS/Slack alert fires on a test trade (check `smsbot` is running on :8001)
- [ ] `crontab -l` shows all jobs
- [ ] `journalctl -u ibc-gateway --since "1 hour ago"` shows Gateway running

---

## Operational commands

```bash
# stop everything
sudo systemctl stop tradingbot-dashboard ibc-gateway
crontab -r    # DESTRUCTIVE — removes ALL cron jobs; use nano -e to selectively remove

# kill switch — halt trading immediately
touch ~/tradingbot/state/KILL
# resume
rm ~/tradingbot/state/KILL

# tail logs
tail -f ~/tradingbot/state/dashboard.log
tail -f ~/tradingbot/state/ibc.log
tail -f ~/tradingbot/state/cron.log

# manual cycle
cd ~/tradingbot && venv/bin/python main.py
```

---

## Going live (only after 2+ weeks of stable paper)

1. Edit `.env`:
   ```
   IBKR_PORT=7496
   IBKR_MODE=live
   ```
   Note: IB login credentials live in `~/ibc/config.ini`, not in `.env`
   (T-ENV-DEAD-KEYS-PRUNE1 removed the misleading `IBKR_USERNAME` /
   `IBKR_PASSWORD` keys that the runtime never read).
2. Edit `~/ibc/config.ini` directly (set IbLoginId, IbPassword, TradingMode):
3. Restart Gateway:
   ```bash
   sudo systemctl restart ibc-gateway
   ```
4. Start with ≤10% of intended live capital — watch the first week closely
5. Never disable the kill switch

---

## Troubleshooting

**Dashboard doesn't respond:**
```bash
sudo journalctl -u tradingbot-dashboard -n 50
```

**IB Gateway keeps restarting:**
- Check `~/tradingbot/state/ibc.log` — usually a credential or Xvfb issue
- Verify `DISPLAY=:99` is set in the service file
- Verify Xvfb is running: `pgrep -f Xvfb`

**`main.py` says "no watchlist":**
```bash
cd ~/tradingbot && venv/bin/python scripts/build_watchlist.py
```

**Stopped at Gate 1 (regime = bear/crash):**
This is correct behavior. The HMM decided the market is in a hostile regime. Check `state/state.json` — `regime` field. Hold existing positions; no new entries.
