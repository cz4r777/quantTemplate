# quantTemplate

Reusable skeleton for systematic quant trading bots.

Forked from a working IBKR-backed options strategy; identifiers and live
state have been scrubbed. Use this as the starting point for a new
strategy repo — clone, rename, swap out the brain/ logic, keep the
safety and ops scaffolding.

## What's in the box

| Layer            | Path                  | Purpose                                                          |
|------------------|-----------------------|------------------------------------------------------------------|
| Strategy logic   | `brain/`              | signal generation, regime detection, breakout/VCP, fundamentals  |
| Sizing           | `allocation/`         | position sizer, portfolio heat                                   |
| Execution        | `execution/`          | post-broker accounting, exit rules, exec log                     |
| Safety           | `safety/`             | portfolio heat ceiling, guardrails                               |
| Broker           | `broker/`             | IBKR client (ib_insync wrapper) with long-only / currency guards |
| Notifications    | `notifications/`      | pure-schema formatter + ntfy/SMS dispatch                        |
| Dashboard        | `dashboard/`          | FastAPI/uvicorn read-only ops UI + gated mutation routes         |
| Scripts          | `scripts/`            | watchlist build, position management, audits, release gate       |
| Backtest         | `backtest/`           | walk-forward engine + drilldowns                                 |
| Tests            | `tests/`              | pytest suite                                                     |
| Deploy           | `deploy/`             | install / setup helpers                                          |
| Orchestration    | `run.sh`              | operator menu (Linux/WSL)                                        |
| Loop             | `main.py`             | one cycle of the bot — invoked by cron or by live_launcher       |

## Quick start

```bash
git clone <your-repo-url> mybot
cd mybot
python -m venv venv
source venv/bin/activate          # or: venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env              # fill in IBKR + FMP keys
pytest -q                         # confirm the suite is green
python main.py                    # one paper cycle (requires IB Gateway on 4002)
```

## What you almost certainly want to change

- **`brain/`** — replace the breakout/VCP/CANSLIM brain with your own
  signal logic. The trend template, stage engine, RS rank, market timing,
  earnings filter, and fundamental gate are decent starting points but
  reflect one specific strategy.
- **`config.py`** — risk knobs (`MAX_POSITIONS`, `RISK_PER_TRADE`),
  symbol universe defaults.
- **`scripts/build_watchlist.py`** — universe source (FFTY / MAG7 /
  SP500-top50 / NDX100 are baked in).
- **`dashboard/app.py`** — operator UI labels.
- **`requirements.txt`** — drop packages you don't use.

## Safety defaults (preserve these)

The template ships with several safety invariants. Removing them is your
choice but read each one first:

- **Paper-only by default.** `IBKR_MODE=paper`. Live mode is refused
  unless every `LIVE_*` marker is set via the live launcher.
- **Long-only stocks** with naked-short refusal at the broker layer.
- **Emergency-gated manual liquidation.** `scripts/sell_position.py`
  requires `--emergency` for non-routine closes.
- **Dashboard mutation routes are token-gated.** `/sell`, `/claim`,
  `/flatten` etc. fail closed without `DASHBOARD_MUTATION_TOKEN`.
- **Loopback-only dashboard bind** unless you explicitly accept LAN
  exposure (see `tools/dashboard_runtime.py`).
- **Notification failure never alters trading behavior.**
- **No state files in git.** `state/` is gitignored.

## What's been scrubbed from the template

- Live account ID → `YOUR_ACCOUNT_ID`
- Paper account ID → `YOUR_PAPER_ACCOUNT_ID`
- All runtime `state/` (positions, decisions, equity history, IB cache)
- Cached market data and third-party PDFs in `data/`
- Project-specific narrative docs (HANDOFF.md, BACKTEST_RESULTS.md,
  framework.MD, SPEC.md)
- Bot venvs and `__pycache__`

## License

Choose one before publishing. The template ships with none.
