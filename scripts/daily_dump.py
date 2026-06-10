"""Daily full dump — single paste-friendly file with everything.

Covers both v1.2 (swing) AND stress-v1.0 (high-frequency stress) since
they share the same paper account.

For a target date (default: today), produces:

  HEADER                — date, account snapshot deltas
  ACCOUNT SUMMARY       — NLV / cash / realized / unrealized
  IB FILLS              — every execution today, chronological
  v1.2 BOT EVENTS       — every decisions.jsonl entry today
  v1.2 BROKER CALLS     — every exec_log.jsonl entry today
  stress-v1.0 EVENTS    — same from stress folder
  PER-SYMBOL AUDIT      — full diagnose for each traded symbol today
  CURRENT POSITIONS     — open positions snapshot
  DANGER FLAGS          — anything that needs operator attention

Output: one text file at state/dump_YYYY-MM-DD.txt — copy/paste to share.

Usage:
  python scripts/daily_dump.py
  python scripts/daily_dump.py --date 2026-04-28
  python scripts/daily_dump.py --no-ib              # skip IB queries (offline mode)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Unique clientId per script invocation. PID-based: each subprocess gets
# a stable id derived from its PID. Avoids Error 326 collision with the
# running bot AND collisions between concurrent ad-hoc scripts (e.g.,
# multiple dashboard Sell clicks within seconds).
import os

os.environ["IBKR_CLIENT_ID"] = str(1000 + (os.getpid() % 9000))

V12_DECISIONS = ROOT / "state" / "decisions.jsonl"
V12_EXEC_LOG = ROOT / "state" / "exec_log.jsonl"
V12_POSITIONS = ROOT / "state" / "positions.json"
STRESS_EXEC_LOG = ROOT.parent / "stress-v1.0" / "state" / "exec_log.jsonl"
STRESS_POSITIONS = ROOT.parent / "stress-v1.0" / "state" / "positions.json"


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _filter_date(rows: list[dict], date_str: str, ts_field: str = "ts") -> list[dict]:
    return [
        r
        for r in rows
        if str(r.get(ts_field, "")).startswith(date_str)
        or str(r.get("bar_time", "")).startswith(date_str)
        or str(r.get("time", "")).startswith(date_str)
    ]


def _section(title: str) -> str:
    return f"\n{'=' * 8} {title} {'=' * max(0, 60 - len(title))}"


def _yfinance_close(symbol: str) -> float:
    """Fallback price from yfinance when IB returns 0 (paper account
    without real-time L1 subscription)."""
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period="2d", interval="1d")
        if df is None or df.empty:
            return 0.0
        return float(df["Close"].iloc[-1])
    except Exception:
        return 0.0


def _ib_data(date_str: str, days: int = 7) -> dict:
    """Pull live IB data: account summary + fills + positions."""
    try:
        from broker.ibkr_client import IBKRClient
    except Exception as e:
        return {"error": f"IB import failed: {e}"}
    out = {"summary": {}, "fills": [], "positions": []}
    try:
        b = IBKRClient()
        b.connect()
        try:
            out["summary"] = b.account_summary()
            all_fills = b.fills(days=days)
            out["fills"] = [f for f in all_fills if str(f["time"]).startswith(date_str)]
            for p in b.ib.positions():
                qty = int(round(float(p.position)))
                if qty == 0:
                    continue
                c = p.contract
                sym = c.symbol
                sec_type = getattr(c, "secType", "STK")
                # Tag option contracts so the renderer doesn't compare
                # contract-cost-per-100-shares to the stock price (which
                # produces the misleading -80% "loss" display).
                if sec_type == "OPT":
                    out["positions"].append(
                        {
                            "symbol": sym,
                            "sec_type": "OPT",
                            "contracts": qty,
                            "avg_cost_per_contract": float(p.avgCost or 0),
                            "strike": float(getattr(c, "strike", 0) or 0),
                            "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
                            "right": getattr(c, "right", "C"),
                        }
                    )
                    continue
                try:
                    px = b.market_price(sym)
                except Exception:
                    px = 0.0
                if px <= 0:
                    px = _yfinance_close(sym)
                out["positions"].append(
                    {
                        "symbol": sym,
                        "sec_type": "STK",
                        "shares": qty,
                        "avg_cost": float(p.avgCost or 0),
                        "market": px,
                    }
                )
        finally:
            b.disconnect()
    except Exception as e:
        out["error"] = f"IB connect failed: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="target date (YYYY-MM-DD), default today",
    )
    ap.add_argument("--no-ib", action="store_true", help="skip IB queries (offline)")
    args = ap.parse_args()

    target = args.date
    lines = []
    lines.append("#" * 70)
    lines.append(
        f"  DAILY DUMP — {target}    generated {dt.datetime.now().isoformat(timespec='seconds')}"
    )
    lines.append("#" * 70)

    # --- IB live data ---
    ib = {} if args.no_ib else _ib_data(target)

    # --- Section: account summary ---
    lines.append(_section("ACCOUNT SUMMARY (live from IB)"))
    if "error" in ib:
        lines.append(f"  ! could not query IB: {ib['error']}")
    elif ib.get("summary"):
        for k, v in ib["summary"].items():
            if isinstance(v, float):
                lines.append(f"  {k:<25} ${v:>15,.2f}")
            else:
                lines.append(f"  {k:<25} {v}")
    else:
        lines.append("  (no data)")

    # --- Section: IB fills today ---
    lines.append(_section(f"IB FILLS ON {target}"))
    fills = ib.get("fills", [])
    if not fills:
        lines.append("  (no fills today)")
    else:
        total_pnl = 0.0
        total_comm = 0.0
        for f in fills:
            ts = f["time"][:19]
            sym = f["symbol"]
            side = f["side"]
            sh = f["shares"]
            px = f["price"]
            comm = f["commission"]
            pnl = f["realized_pnl"]
            total_pnl += pnl
            total_comm += comm
            mk = "+" if pnl > 0 else ("-" if pnl < 0 else " ")
            lines.append(
                f"  {ts}  {sym:<6} {side:<3} {sh:>5} @ ${px:>8.2f}  "
                f"comm ${comm:>5.2f}  P&L {mk}${abs(pnl):>9.2f}"
            )
        lines.append(
            f"  {'TOTAL':<22}  {len(fills)} fills  comm ${total_comm:>5.2f}  P&L ${total_pnl:+.2f}"
        )

    # --- Section: v1.2 bot events ---
    lines.append(_section(f"v1.2 BOT EVENTS (decisions.jsonl, {target})"))
    v12_decisions = _filter_date(_read_jsonl(V12_DECISIONS), target)
    if not v12_decisions:
        lines.append("  (no events)")
    else:
        # Summarize by event type, then list non-skip events in detail
        by_event = {}
        for d in v12_decisions:
            by_event.setdefault(d.get("event", "?"), []).append(d)
        lines.append("  event breakdown:")
        for ev, es in sorted(by_event.items(), key=lambda x: -len(x[1])):
            lines.append(f"    {ev:<18} {len(es)}")
        lines.append("  non-skip events:")
        for d in v12_decisions:
            if d.get("event") == "skip":
                continue
            ts = d.get("ts", "")[:19]
            extras = " ".join(
                f"{k}={v}" for k, v in d.items() if k not in ("ts", "event", "symbol")
            )
            ev = d.get("event") or "?"
            sym = d.get("symbol") or "-"
            lines.append(f"    {ts}  {ev:<10}  {sym:<6}  {extras}")

    # --- Section: v1.2 broker calls ---
    lines.append(_section(f"v1.2 BROKER CALLS (exec_log.jsonl, {target})"))
    v12_execs = _filter_date(_read_jsonl(V12_EXEC_LOG), target)
    if not v12_execs:
        lines.append("  (no broker calls — exec_log only has data since the last code restart)")
    else:
        for e in v12_execs:
            ts = e.get("ts", "")[:19]
            action = e.get("action", "?")
            payload = e.get("payload", {}) or {}
            sym = e.get("symbol", "-")
            lines.append(
                f"  {ts}  {action:<18}  {sym:<6}  "
                f"status={payload.get('status', '?')}  "
                f"delta={payload.get('delta', '?')}  "
                f"fill={payload.get('fill_price', '?')}"
            )

    # --- Section: stress-v1.0 events ---
    lines.append(_section(f"stress-v1.0 EVENTS ({target})"))
    if not STRESS_EXEC_LOG.exists():
        lines.append("  (stress-v1.0 not deployed or no exec_log yet)")
    else:
        stress_events = _filter_date(_read_jsonl(STRESS_EXEC_LOG), target)
        if not stress_events:
            lines.append("  (no events today)")
        else:
            by_action = {}
            for e in stress_events:
                by_action.setdefault(e.get("action", "?"), 0)
                by_action[e.get("action", "?")] += 1
            lines.append("  action breakdown:")
            for a, n in sorted(by_action.items(), key=lambda x: -x[1]):
                lines.append(f"    {a:<18} {n}")
            lines.append(f"  total: {len(stress_events)} events")

    # --- Section: per-symbol audits ---
    # Restrict to symbols the V1.2 BOT actually traded (decision events
    # other than skip/cycle_start). Stress's daily watchlist round-trips
    # would otherwise produce pages of noise per symbol.
    bot_traded_syms = sorted(
        {
            d.get("symbol")
            for d in v12_decisions
            if d.get("symbol")
            and d.get("event") not in ("skip", "cycle_start", "position_reconciliation")
        }
    )
    fill_syms = sorted({f["symbol"] for f in fills if f["symbol"] in bot_traded_syms})
    if fill_syms:
        lines.append(_section(f"PER-SYMBOL AUDITS ({len(fill_syms)} bot-traded symbols)"))
        try:
            from scripts.trade_diagnose import diagnose
        except Exception as e:
            lines.append(f"  (could not import diagnose: {e})")
        else:
            for sym in fill_syms:
                try:
                    lines.append(diagnose(sym, save=False))
                except Exception as e:
                    lines.append(f"\n!! diagnose failed for {sym}: {e}")

    # --- Section: current positions ---
    # Split STK from OPT — option avgCost is per-contract (premium × 100),
    # not per-share. Comparing it to stock price gives a meaningless % loss.
    lines.append(_section("CURRENT OPEN POSITIONS"))
    pos = ib.get("positions", [])
    stocks = [p for p in pos if p.get("sec_type", "STK") == "STK"]
    options = [p for p in pos if p.get("sec_type") == "OPT"]
    if not pos:
        lines.append("  (no open positions or IB unavailable)")

    if stocks:
        total_pnl = 0.0
        lines.append("  -- STOCKS --")
        for p in stocks:
            sym = p["symbol"]
            sh = p.get("shares", 0)
            cost = p.get("avg_cost", 0)
            mk = p.get("market", 0)
            pnl = (mk - cost) * sh if cost > 0 and mk > 0 else 0.0
            pct = (mk / cost - 1) * 100 if cost > 0 else 0.0
            total_pnl += pnl
            mark = "+" if pnl >= 0 else "-"
            lines.append(
                f"  {sym:<6} {sh:>6} sh @ ${cost:>9.2f}  "
                f"now ${mk:>9.2f}  unreal {mark}${abs(pnl):>10,.2f} ({pct:+.1f}%)"
            )
        lines.append(f"  {'STOCK TOTAL':<10}{'':<28}{'':<22} ${total_pnl:+,.2f}")

    if options:
        lines.append("  -- OPTIONS --  (cost is per-contract = premium × 100)")
        for p in options:
            sym = p["symbol"]
            n = p.get("contracts", 0)
            cost = p.get("avg_cost_per_contract", 0)
            strike = p.get("strike", 0)
            expiry = p.get("expiry", "")
            right = p.get("right", "C")
            implied_premium = cost / 100.0 if cost > 0 else 0.0
            lines.append(
                f"  {sym:<6} {n:>3}x ${strike:>7.2f}{right} {expiry}  "
                f"@ ${implied_premium:>6.2f} premium  (cost ${cost:>9.2f}/contract)"
            )
        lines.append(
            "  (option P&L not shown — needs current premium re-pricing; see dashboard :8082)"
        )

    # --- Section: danger flags (STOCKS only — option metric is misleading) ---
    lines.append(_section("DANGER FLAGS"))
    flags = []
    for p in stocks:
        sym = p["symbol"]
        cost = p.get("avg_cost", 0)
        mk = p.get("market", 0)
        if cost > 0 and mk > 0:
            pct = (mk / cost - 1) * 100
            if pct < -8:
                flags.append(f"!! {sym} down {pct:+.1f}% from cost — review")
            elif pct < -5:
                flags.append(f"!  {sym} down {pct:+.1f}% from cost")
    if not flags:
        flags.append("  (none)")
    for f in flags:
        lines.append(f"  {f}")

    out = "\n".join(lines)

    out_path = ROOT / "state" / f"dump_{target}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(out)
    print(f"\n{'=' * 70}")
    print(f"  saved: {out_path.relative_to(ROOT)}")
    print(f"  {'cat ' + str(out_path.relative_to(ROOT)) + ' | xclip -selection clipboard'}")
    print("  (or just open in editor and copy/paste to share)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
