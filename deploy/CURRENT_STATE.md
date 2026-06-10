# Current Server State — Single Source of Truth

If you're a human or AI picking this up fresh, read this file first.

## TL;DR — the one command that fixes everything

```bash
bash ~/code/tradingbot/deploy/setup.sh
```

That's it. Idempotent — safe to run daily, safe to run after rsync, safe to run after a crash. It:
1. Activates venv, installs/updates Python deps if needed
2. Renders IBC config from `.env`
3. Rebuilds SP500 top-50 (only if > 5 days old)
4. Rebuilds the watchlist (FFTY + MAG7 + SP500 top-50)
5. Installs cron jobs (idempotent — won't duplicate)
6. Reports state and what (if anything) you need to do manually

After `setup.sh` exits, if it reports "Gateway not listening" or "dashboard not running," it tells you the exact command to run.

## Host

- **Server IP:** `192.168.1.99`
- **OS:** Kali (Debian-based) running under VMware, with desktop environment
- **User:** `kali`
- **SSH:** `ssh kali@192.168.1.99` (key-based, no password)

## Glossary — what everything means

| Term | What it is |
|---|---|
| `192.168.1.99` | IP address of the Debian VM on your LAN |
| `kali` | Your Linux user account on the VM |
| `kali@192.168.1.99` | SSH target — "log into 192.168.1.99 as kali" |
| `~` | Your home directory = `/home/kali` |
| `~/code/tradingbot` | The bot's code lives here on the server |
| `~/Jts` | Where IB Gateway stores its runtime settings/logs |
| `~/ibgateway` (or `~/Jts/ibgateway/`) | Where Gateway is installed |
| `.env` | File in the repo holding IBKR login + port config |
| `state/` | Folder where the bot writes its runtime data (positions, events, quotes) |
| `venv/` | Python virtual environment with all the dependencies |
| **IB Gateway** | The Java app that connects to IBKR — the bot talks to it via port 4002 (paper) |
| **Port 4002** | Gateway Paper API port. Bot connects here. 4001=Live. |
| **Port 7497** | TWS Paper API port (different app than Gateway). 7496=Live. |
| **Port 8080** | Dashboard (uvicorn) |
| **IBC** | Optional auto-restart tool for Gateway; currently NOT used |
| **FFTY** | Innovator IBD 50 ETF — 50 stocks IBD rates as top growth |
| **MAG 7** | Apple, Microsoft, Google, Amazon, Meta, Nvidia, Tesla |
| **SP500 top-50** | Top 50 S&P 500 stocks by 6-month relative strength (our computation) |
| **Paper trading** | Simulated trading with fake money — IBKR provides $1M default to practice |
| **rsync** | Command to push files from Windows → server. Only used if you're NOT editing on server directly via VS Code Remote-SSH. |
| **cron** | Linux's scheduled task system. Runs `main.py` every 15 min during market hours. |
| **uvicorn** | Web server running the dashboard. Python process, listens on :8080. |
| **`main.py`** | One-shot script. Connects to Gateway, runs one trading cycle, disconnects, exits. Cron fires it every 15 min. |
| **`dashboard/app.py`** | Long-running web server. Reads state files, serves HTTP. |

## GitHub vs SSH — NOT the same thing

- **SSH to your server:** `ssh kali@192.168.1.99` → your local Debian VM on your LAN
- **GitHub:** a cloud service at `github.com` for git repos — we're NOT using it for this project (VS Code Remote-SSH edits files directly on the server, no git required)

## Directory layout (as-installed — DO NOT reorganize)

```
/home/kali/
├── code/
│   ├── tradingbot/              ← this repo, rsync'd from workstation
│   │   ├── .env                 ← IBKR creds + connection params (gitignored)
│   │   ├── venv/                ← Python virtual env
│   │   ├── state/               ← runtime state (positions.json, equity_history.jsonl, etc.)
│   │   └── ...
│   └── ibc/                     ← IBC Alpha (installed but NOT USED for initial testing)
│       └── config.ini           ← rendered from .env via deploy/render_ibc_config.sh
│
├── Jts/
│   ├── jts.ini                  ← Gateway runtime settings
│   └── ibgateway/
│       └── 1037/
│           └── ibgateway1       ← THE GATEWAY BINARY
│                                  (named ibgateway1, not ibgateway, because
│                                  an earlier install at ~/ibgateway created
│                                  a naming conflict. Don't rename it, don't
│                                  symlink it. Just use the binary as-is.)
```

**Rule: do NOT move, rename, or symlink anything in this layout. It is what the installer produced. Work with it.**

## How to start Gateway (manual, no IBC)

```bash
~/Jts/ibgateway/1037/ibgateway1 &
```

Or use the desktop shortcut in the Debian menu (installer placed one automatically).

A login dialog appears:
1. Select **Paper Trading** (radio button at bottom)
2. Enter username + password from `~/code/tradingbot/.env`
3. Click **Log in**
4. Window changes to "Connection: OK, API enabled on port 7497"

Leave Gateway running. It's fine if it's minimized.

## How to start the bot

```bash
cd ~/code/tradingbot
source venv/bin/activate
python main.py          # runs one cycle; prints regime/positions/heat line
```

Or start the dashboard and keep it running:

```bash
uvicorn dashboard.app:app --host 0.0.0.0 --port 8080 &
```

Then open `http://192.168.1.99:8080` from any machine on the LAN.

## How to test the full chain

```bash
python -c "
from broker.ibkr_client import IBKRClient
b = IBKRClient()
b.connect()
print(f'equity: \${b.equity():,.2f}')
b.disconnect()
"
```

Should print paper equity (~$1,000,000 default) and exit cleanly.

## What is NOT set up yet

- **IBC auto-restart** — Gateway will log itself out at ~3:45 AM ET nightly. For now, log in manually each morning. IBC wiring is a later step.
- **systemd services** — none are enabled. Start dashboard manually.
- **Cron jobs** — `deploy/crontab` has the scheduled entries but nothing is installed via `crontab` yet.

## Configuration locations (canonical)

| What | Where |
|---|---|
| IBKR paper creds | `~/code/tradingbot/.env` |
| Bot risk parameters | `~/code/tradingbot/config.py` |
| Trading rules (human-readable) | `~/code/tradingbot/data/playbook/rules.md` |
| Watchlist tickers | `~/code/tradingbot/state/watchlist.json` |
| Runtime positions | `~/code/tradingbot/state/positions.json` |
| Event audit log | `~/code/tradingbot/state/decisions.jsonl` |
| Equity history | `~/code/tradingbot/state/equity_history.jsonl` |
| Gateway session log | `~/Jts/log.<YYYYMMDD>.txt` |

## Common problems and where to look

| Symptom | Check |
|---|---|
| Gateway login "Invalid username or password" | `~/Jts/log.$(date +%Y%m%d).txt` — look for `INVALID_USERNAME_OR_BAD_IP` (means paper account not propagated, wait 24h from activation) |
| Gateway UI freezes | VM may be swapping. `free -h`, `vmstat 1 5`. Bot keeps running even if GUI freezes. |
| Bot can't connect to Gateway | Is Gateway logged in? `ss -tlnp \| grep 7497` should show listening |
| Dashboard shows blank | `state/state.json` hasn't been written yet — run `python main.py` once |
| Tuning / backtest results vary | Expected. Date delta, watchlist refresh, HMM weekly refitting. Sharpe should stay stable; total return can fluctuate ±10% |

## Useful logs

```bash
tail -f ~/Jts/log.$(date +%Y%m%d).txt            # Gateway session
tail -f ~/code/tradingbot/state/decisions.jsonl   # bot decisions
tail -f ~/code/tradingbot/state/dashboard.log     # dashboard (if running via systemd)
```

## Do NOT

- Do not reorganize `~/Jts/ibgateway/` — binary name `ibgateway1` is expected
- Do not move files to match some "cleaner" layout — this IS the clean layout
- Do not enable IBC auto-restart until the bot has run stable for at least a week of paper
- Do not skip the playbook rules in `data/playbook/rules.md` — those are the spec, code must match
