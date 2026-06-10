# Start-from-Zero Install Guide

Complete step-by-step for setting up a fresh VM from scratch. Use this any time the server gets messy — it's faster than debugging.

**Estimated total time:** 45-60 minutes.

---

## Phase 1 — New VM in VMware (10 min)

### 1.1 Create the VM

VMware Workstation → **File → New Virtual Machine** → Typical

- **Install from:** Installer disc image file (ISO) → point at **Ubuntu Desktop 22.04 LTS** ISO (download from ubuntu.com if you don't have it)
- **Name:** `tradingbot`
- **Location:** `C:\VMs\tradingbot` (NOT in OneDrive/Documents — prevents cloud sync eating disk)
- **Disk size:** 40 GB, **split into multiple files**, **do NOT preallocate**
- **Customize Hardware:**
  - Memory: **4096 MB** (4 GB)
  - Processors: **2** cores
  - Network: **Bridged** (so the VM gets its own LAN IP)
  - Display: Accelerate 3D graphics **unchecked** (prevents GUI freezes)
- Finish → power on

### 1.2 Ubuntu install

Take every default. Critical choices:
- **User name:** `trader` (simpler than `kali`)
- **Computer name:** `tradingbot`
- **Password:** something you'll remember (this is your SSH + sudo password)
- **Login automatically** ← ✅ check this so the desktop auto-starts
- **Install third-party software** ← ✅ check

Wait for install (~15 min). Reboot when prompted.

### 1.3 First login + VMware Tools

Log into the desktop. Open a terminal (Ctrl+Alt+T).

```
sudo apt update && sudo apt install -y open-vm-tools-desktop git openssh-server
sudo systemctl enable --now ssh
```

This gives you:
- Clipboard integration with Windows host (no more "paste doesn't work")
- Git (for the bot repo)
- SSH server so you can connect from your workstation

### 1.4 Get your server IP

```
hostname -I | awk '{print $1}'
```

Write it down. Probably `192.168.1.xx`. **This replaces `192.168.1.99` in all future commands.**

### 1.5 From your Windows workstation, test SSH

```
ssh trader@<that-ip>
```

Should log you in with your password. If yes, the server is reachable.

---

## Phase 2 — Install IB Gateway (10 min)

### 2.1 Download Gateway (on the VM's desktop, via Firefox)

Open Firefox → go to `https://www.interactivebrokers.com/en/trading/ibgateway-stable.php` → download **IB Gateway** (Linux 64-bit .sh installer).

### 2.2 Install to the DEFAULT path

Terminal:

```
cd ~/Downloads
chmod +x ibgateway-stable-standalone-linux-x64.sh
./ibgateway-stable-standalone-linux-x64.sh
```

**Press Enter at EVERY prompt** — take defaults. Don't customize the install path, or IBC paths get weird.

Installer creates: `~/Jts/ibgateway/<version>/ibgateway`

### 2.3 Test launch

Desktop menu → search "IB Gateway" → launch. Login dialog should appear.

**Don't log in yet** — we'll do that after the bot is ready.

Close Gateway for now.

---

## Phase 3 — Deploy the bot (10 min)

### 3.1 Push code from your Windows workstation

In Git Bash on Windows:

```
rsync -avz --exclude venv --exclude state --exclude __pycache__ \
  /c/Users/z/Desktop/code/tradingbot/ trader@<server-ip>:~/code/tradingbot/
```

Or use VS Code's Remote-SSH: open the server folder, drag files in.

### 3.2 Set up .env with paper credentials

On the server:

```
cd ~/code/tradingbot
cp .env.example .env
nano .env
```

Edit these fields (leave others as defaults for paper):

```
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=1
IBKR_MODE=paper
# IB login credentials live in ~/ibc/config.ini (NOT in .env).
# T-ENV-DEAD-KEYS-PRUNE1 removed IBKR_USERNAME / IBKR_PASSWORD from
# templates because runtime code never read them.
```

Save (Ctrl+O, Enter, Ctrl+X). Lock down permissions:

```
chmod 600 .env
```

### 3.3 Run the one-command setup

```
bash ~/code/tradingbot/deploy/setup.sh
```

This:
- Creates venv, installs Python deps (~3 min first time)
- Renders IBC config
- Builds SP500 top-50
- Builds watchlist
- Installs cron jobs
- Reports what's ready and what's not

At the end, it'll say "Gateway not listening" — that's expected, we haven't logged in yet.

---

## Phase 4 — Connect Gateway + verify bot (10 min)

### 4.1 Launch Gateway, log in

Desktop menu → IB Gateway → login dialog:

- Select **Paper Trading** radio button (NOT Live)
- Username: your paper username
- Password: your paper password
- Click Login

Watch for "Connected" in the top-right of the Gateway window.

### 4.2 Enable the API

Gateway menu → **Configure → Settings → API → Settings** (path varies by version; may be under **File → Global Configuration**):

- ☑ **Enable ActiveX and Socket Clients**
- Socket port: **4002**
- Trusted IPs: `127.0.0.1`
- ☐ **Read-Only API** (must be UNCHECKED)
- Click **Apply**, then **OK**

### 4.3 Verify port is listening

Terminal:

```
ss -tlnp | grep 4002
```

Should show `LISTEN 0 50 127.0.0.1:4002 ... java`. If empty, API settings didn't save — repeat 4.2, making sure to click Apply.

### 4.4 Run the bot

```
cd ~/code/tradingbot && source venv/bin/activate && python main.py
```

Expected output:
```
regime=bull positions=0 heat=0.00%
```

If `ConnectionRefused` → API port didn't open, repeat 4.2.
If other error → paste it and debug.

### 4.5 Start the dashboard

```
nohup uvicorn dashboard.app:app --host 0.0.0.0 --port 8080 > state/dashboard.log 2>&1 &
```

Open `http://<server-ip>:8080` in your Windows browser. Dashboard populates with equity, regime, watchlist.

---

## Phase 5 — Automation (5 min)

### 5.1 Cron is already installed

`setup.sh` installed these:
- `main.py` every 15 min during market hours (Mon-Fri)
- Weekly watchlist refresh (Mon 8:55am)
- Weekly SP500 top-50 refresh (Sun 8pm)

Verify:

```
crontab -l
```

You should see entries tagged `# tradingbot-managed`.

### 5.2 Make the dashboard auto-start

Create a systemd user service so it survives reboots:

```
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/dashboard.service <<'EOF'
[Unit]
Description=Trading bot dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/trader/code/tradingbot
ExecStart=/home/trader/code/tradingbot/venv/bin/uvicorn dashboard.app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now dashboard
```

Dashboard now restarts automatically if it crashes, and starts on every reboot.

### 5.3 Prevent the VM screen from locking

Ubuntu Settings → Privacy → Screen Lock → **Automatic Screen Lock: Off**.
Ubuntu Settings → Power → Screen Blank → **Never**.

This keeps the Gateway GUI visible indefinitely.

---

## Phase 6 — Daily operation

### What runs automatically

- Cron fires `main.py` every 15 min during market hours
- Cron refreshes watchlist every Monday
- Dashboard is always up at `http://<server-ip>:8080`

### What you do manually

- **Every morning (~9am ET):** log into Gateway manually (it logs out nightly).
  - Desktop icon → login dialog → paper creds → Log In
  - Verify "Connected" top-right
- **Check dashboard during the day** to see positions, heat, recent events

### Weekly

- Run `bash deploy/setup.sh` once a week — it refreshes everything that needs refreshing and reports state

### To push bot code updates from workstation

```
rsync -avz --exclude venv --exclude state --exclude __pycache__ \
  /c/Users/z/Desktop/code/tradingbot/ trader@<server-ip>:~/code/tradingbot/
```

Then on the server: `bash deploy/setup.sh` (picks up any new deps and rebuilds watchlist).

---

## Snapshot strategy (important)

**Take ONE snapshot now, right after everything above works.** Label it "bootstrap-complete". This is your rollback point.

**Never stack more than 2-3 snapshots.** Delete old ones after a week. Snapshots chain-corrupt when left alive.

**Real backup = rsync the code + .env from workstation.** Not snapshots.

---

## Emergency recovery

If everything goes weird:

```
# On server
pkill -f ibgateway
pkill -f uvicorn
pkill -f python

# Re-run setup
bash ~/code/tradingbot/deploy/setup.sh

# Launch Gateway, log in, verify
# Then restart dashboard
systemctl --user restart dashboard
```

If the VM itself is broken: **revert to your "bootstrap-complete" snapshot**.

If that's also broken: fresh VM from Phase 1. Takes 45 min. You have the .env on your workstation so recovery is clean.
