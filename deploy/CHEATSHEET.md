# Daily Cheat Sheet

Print this. Or bookmark it. Everything you need in order.

---

## Every morning — 3 commands

### 1. Log into Gateway

Double-click the **IB Gateway** icon on the desktop menu.
- **Paper Trading** radio selected
- Creds from `.env`
- Click **Log In** → wait for "Connected" top-right

### 2. Clean start — one command

```
bash ~/code/tradingbot/deploy/start.sh
```

Kills any stale processes, starts the dashboard cleanly, runs one cycle, reports state. No "Client ID 1 already in use" errors — it clears them first.

### 3. Open dashboard

```
http://<server-ip>:8080
```

That's it. Cron runs the bot every 15 min automatically.

---

## End of day — one command (optional)

```
bash ~/code/tradingbot/deploy/stop.sh
```

Cleanly stops the bot + dashboard. Leaves Gateway running (it'll log itself out at 3:45 AM ET regardless). Running `stop.sh` before `start.sh` next time guarantees a clean slate — no stale client IDs.

**You can skip `stop.sh`** if you just reboot the VM — a reboot kills everything cleanly anyway.

---

## Checking in during the day

### See what the bot just decided

```
tail -f ~/code/tradingbot/state/decisions.jsonl
```

Ctrl+C to stop tailing.

### See recent trade/skip events

Open the dashboard → Events panel.

### Find today's breakouts across the whole market

```
cd ~/code/tradingbot && source venv/bin/activate && python scripts/find_breakouts.py
```

Prints a sorted table.

### Manual one-shot cycle (force a bot run now)

```
cd ~/code/tradingbot && source venv/bin/activate && python main.py
```

---

## If something's wrong

### Dashboard blank / shows "loading…"

```
pkill -f uvicorn
cd ~/code/tradingbot && nohup venv/bin/uvicorn dashboard.app:app --host 0.0.0.0 --port 8080 > state/dashboard.log 2>&1 &
```

### Bot fails with "ConnectionRefused 127.0.0.1:4002"

Gateway isn't logged in or API isn't enabled.

1. Check Gateway window — is it logged in? "Connected" top-right?
2. Gateway → File → Global Configuration → API → Settings
3. ✅ Enable ActiveX and Socket Clients
4. ❌ Read-Only API (MUST be unchecked)
5. Click **Apply** + OK
6. Verify port: `ss -tlnp | grep 4002`

### Gateway crashed / won't log in

```
pkill -f ibgateway; sleep 3
```

Then relaunch Gateway from desktop menu. Log in again.

### "wrong password" when you know it's right

- Rate limit. Wait 60 minutes. Don't keep trying.
- Then ONE careful attempt.

### Dashboard shows old data from yesterday

Cron fires main.py every 15 min during market hours. If it's a weekend or pre-market, that's expected. Run `python main.py` manually to force a cycle.

### VM locking up / heavy RAM

```
free -h
```

If `used` > 8 GB inside a 4 GB VM → something's wrong; restart VM.
If `used` < 3 GB but Windows host shows 30 GB → snapshot bloat. Consolidate snapshots.

### Nothing works / system feels broken

```
bash ~/code/tradingbot/deploy/setup.sh
```

It's idempotent. Re-running it always makes things right.

---

## Commands you'll use most

| What | Command |
|---|---|
| All-in-one health check + refresh | `bash ~/code/tradingbot/deploy/setup.sh` |
| Run one trading cycle now | `cd ~/code/tradingbot && source venv/bin/activate && python main.py` |
| Start dashboard | `cd ~/code/tradingbot && nohup venv/bin/uvicorn dashboard.app:app --host 0.0.0.0 --port 8080 > state/dashboard.log 2>&1 &` |
| Kill dashboard | `pkill -f uvicorn` |
| Tail bot decisions | `tail -f ~/code/tradingbot/state/decisions.jsonl` |
| Tail Gateway logs | `tail -f ~/Jts/log.$(date +%Y%m%d).txt` |
| Check API port is open | `ss -tlnp \| grep 4002` |
| Scan for breakouts now | `cd ~/code/tradingbot && source venv/bin/activate && python scripts/find_breakouts.py` |
| Kill Gateway | `pkill -f ibgateway` |
| Show cron jobs | `crontab -l` |
| Kill everything trading-related | `pkill -f uvicorn; pkill -f ibgateway; pkill -f "python main.py"` |

---

## End of day

**Do nothing.** Cron keeps running, Gateway stays logged in until IBKR's nightly logout (~3:45 AM ET).

Tomorrow morning: log into Gateway again (step 2 above). Everything else runs itself.

---

## Once a week

**Monday 9am-ish** — run setup.sh to confirm weekly watchlist refresh happened:

```
bash ~/code/tradingbot/deploy/setup.sh
```

**Friday after close** — optional: review `state/decisions.jsonl` and dashboard to see what fired.

---

## Emergency stop — halt all trading

```
touch ~/code/tradingbot/state/KILL
```

Kill switch file. Next cron cycle sees it → bot halts, no new trades, no exits of existing positions. Existing orders unaffected.

To resume:

```
rm ~/code/tradingbot/state/KILL
```

---

## Where things live

| Need to find... | Path |
|---|---|
| Bot config (risk params) | `~/code/tradingbot/config.py` |
| Credentials | `~/code/tradingbot/.env` |
| Today's positions | `~/code/tradingbot/state/positions.json` |
| Bot decisions (audit) | `~/code/tradingbot/state/decisions.jsonl` |
| Gateway logs | `~/Jts/log.<YYYYMMDD>.txt` |
| Dashboard logs | `~/code/tradingbot/state/dashboard.log` |
| Cron logs | `~/code/tradingbot/state/cron.log` |
| Trading rules (human) | `~/code/tradingbot/data/playbook/rules.md` |

---

## For any future AI helping you

Point them at: `~/code/tradingbot/deploy/CURRENT_STATE.md` and this cheat sheet. They'll know everything needed.
