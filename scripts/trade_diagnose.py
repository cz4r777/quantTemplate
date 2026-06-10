"""Per-trade audit — compiles a single comprehensive report on a symbol.

For each trade or position, pulls from EVERY available source and produces
one paste-friendly report covering:
  * Why it triggered (gate state, trend template gates, breakout details)
  * Compliance check (within MAX_POSITIONS, heat ceiling, risk budget)
  * Position lifecycle (stops moved, pyramid advances, peak)
  * Current state (price vs stop, unrealized P&L, days held)
  * Danger flags (close to stop, position older than typical, etc.)

Sources:
  state/decisions.jsonl    — bot decisions (skip / pilot / exit / trim / pyramid)
  state/exec_log.jsonl     — broker rebalance() calls with full payload
  state/positions.json     — bot's current tracked positions
  IB Gateway (live)        — actual broker positions + recent fills

Output:
  Console + state/audit_<SYMBOL>.txt (for copy-paste / sharing)

Usage:
  python scripts/trade_diagnose.py --symbol NVDA
  python scripts/trade_diagnose.py --all-open       # report on every open position
  python scripts/trade_diagnose.py --recent 3       # last 3 closed round trips
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

DECISIONS_PATH = ROOT / "state" / "decisions.jsonl"
EXEC_LOG_PATH = ROOT / "state" / "exec_log.jsonl"
POSITIONS_PATH = ROOT / "state" / "positions.json"


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


def _filter_symbol(rows: list[dict], symbol: str) -> list[dict]:
    return [r for r in rows if r.get("symbol") == symbol]


def _find_open_symbols() -> list[str]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        return list(json.loads(POSITIONS_PATH.read_text()).keys())
    except json.JSONDecodeError:
        return []


def _ib_snapshot(symbol: str, days: int = 365) -> dict:
    """Pull live position + recent fills from IB for this symbol.

    Defaults to 365 days lookback so we can find positions opened weeks/months ago.
    """
    try:
        from broker.ibkr_client import IBKRClient
    except Exception as e:
        return {"error": f"IB import failed: {e}"}
    out = {"shares": 0, "avg_cost": 0.0, "market_price": 0.0, "fills": []}
    try:
        b = IBKRClient()
        b.connect()
        try:
            for p in b.ib.positions():
                if p.contract.symbol == symbol:
                    out["shares"] = int(p.position)
                    out["avg_cost"] = float(p.avgCost or 0)
                    break
            try:
                out["market_price"] = b.market_price(symbol)
            except Exception as e:
                out["market_price_error"] = f"{type(e).__name__}: {e}"
            all_fills = b.fills(days=days)
            out["fills"] = [f for f in all_fills if f["symbol"] == symbol]
        finally:
            b.disconnect()
    except Exception as e:
        out["error"] = f"IB connect failed: {e}"
    return out


def _search_all_versions(symbol: str) -> dict:
    """Grep every sibling version's decisions.jsonl + exec_log.jsonl for the symbol.
    Reports which bot version (if any) traded it.
    """
    versions_root = ROOT.parent
    results = {}
    for vdir in sorted(versions_root.iterdir()):
        if not vdir.is_dir() or vdir.name.startswith("."):
            continue
        decisions = vdir / "state" / "decisions.jsonl"
        exec_log = vdir / "state" / "exec_log.jsonl"
        v_results = {"decisions": [], "exec_log": []}
        for p, key in [(decisions, "decisions"), (exec_log, "exec_log")]:
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip() or symbol not in line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("symbol") == symbol:
                        v_results[key].append(row)
                except json.JSONDecodeError:
                    continue
        if v_results["decisions"] or v_results["exec_log"]:
            results[vdir.name] = v_results
    return results


def _section(title: str) -> str:
    return f"\n{'=' * 8} {title} {'=' * max(0, 60 - len(title))}"


def _format_decision(d: dict) -> str:
    ts = d.get("ts", "")[:19]
    event = d.get("event", "?")
    extras = []
    for k, v in d.items():
        if k in ("ts", "event", "symbol"):
            continue
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        extras.append(f"{k}={v}")
    return f"  {ts}  {event:<10}  {' '.join(extras)}"


def _format_exec(e: dict) -> str:
    ts = e.get("ts", "")[:19]
    action = e.get("action", "?")
    payload = e.get("payload", {}) or {}
    fill_p = payload.get("fill_price", payload.get("price", "?"))
    delta = payload.get("delta", "?")
    status = payload.get("status", "?")
    notes = e.get("notes", "")
    return f"  {ts}  {action:<18}  delta={delta} fill={fill_p} status={status} {notes}"


def _danger_flags(symbol: str, ib: dict, tracked: dict) -> list[str]:
    flags = []
    shares = ib.get("shares", 0)
    avg_cost = ib.get("avg_cost", 0.0)
    market = ib.get("market_price", 0.0)

    if tracked:
        tracked_shares = tracked.get("shares", 0)
        if tracked_shares != shares:
            flags.append(
                f"!! TRACKING DIVERGENCE: state has {tracked_shares} shares, IB has {shares}"
            )

    if shares != 0 and avg_cost > 0 and market > 0:
        pnl_pct = (market / avg_cost - 1) * 100
        if pnl_pct < -5:
            flags.append(f"!! POSITION DOWN {pnl_pct:+.1f}% from cost")
        elif pnl_pct < -3:
            flags.append(f"! position down {pnl_pct:+.1f}% — watch closely")

        stop = tracked.get("stop", 0)
        if stop > 0 and market > 0:
            buffer_pct = (market / stop - 1) * 100
            if buffer_pct < 0:
                flags.append(
                    f"!! BELOW STOP: market ${market:.2f} < stop ${stop:.2f} "
                    "— exit pending next cycle"
                )
            elif buffer_pct < 2:
                flags.append(f"! within {buffer_pct:.1f}% of stop ${stop:.2f}")

    entry_date = tracked.get("entry_date") if tracked else None
    if entry_date:
        days_held = None
        try:
            d_entry = dt.date.fromisoformat(entry_date[:10])
            days_held = (dt.date.today() - d_entry).days
        except Exception:
            days_held = None
        if days_held is not None and days_held > 60:
            flags.append(f"! position age {days_held}d — typical Stage 2 holds 4-12 weeks")
    return flags


def diagnose(symbol: str, save: bool = True) -> str:
    decisions_all = _read_jsonl(DECISIONS_PATH)
    exec_all = _read_jsonl(EXEC_LOG_PATH)
    decisions = _filter_symbol(decisions_all, symbol)
    execs = _filter_symbol(exec_all, symbol)

    tracked = {}
    if POSITIONS_PATH.exists():
        try:
            tracked = json.loads(POSITIONS_PATH.read_text()).get(symbol, {}) or {}
        except json.JSONDecodeError:
            tracked = {}

    ib = _ib_snapshot(symbol)

    lines = [
        f"\n{'#' * 70}",
        f"  TRADE AUDIT: {symbol}    generated {dt.datetime.now().isoformat(timespec='seconds')}",
        f"{'#' * 70}",
    ]

    # --- Section: current state ---
    lines.append(_section("CURRENT STATE"))
    if "error" in ib:
        lines.append(f"  ! could not query IB: {ib['error']}")
    shares = ib.get("shares", 0)
    avg_cost = ib.get("avg_cost", 0.0)
    market = ib.get("market_price", 0.0)
    if shares != 0:
        pnl = (market - avg_cost) * shares if (market and avg_cost) else 0.0
        pnl_pct = (market / avg_cost - 1) * 100 if avg_cost > 0 else 0.0
        lines.append(f"  IB position:    {shares} shares @ avg ${avg_cost:.2f}")
        lines.append(
            f"  Market price:   ${market:.2f}    Unrealized: ${pnl:+,.2f} ({pnl_pct:+.2f}%)"
        )
    else:
        lines.append("  IB position:    NONE (closed or never opened)")

    if tracked:
        lines.append(
            f"  Bot tracking:   shares={tracked.get('shares', 0)}  "
            f"entry={tracked.get('entry', '?')}  "
            f"stop={tracked.get('stop', '?')}  "
            f"layer={tracked.get('layer', '?')}  "
            f"peak={tracked.get('peak', '?')}"
        )
        if tracked.get("entry_date"):
            lines.append(f"  Entry date:     {tracked['entry_date']}")
    else:
        lines.append("  Bot tracking:   no record in positions.json")

    # --- Section: danger flags ---
    flags = _danger_flags(symbol, ib, tracked)
    if flags:
        lines.append(_section("DANGER FLAGS"))
        for f in flags:
            lines.append(f"  {f}")

    # --- Section: bot decisions for this symbol ---
    lines.append(_section("BOT DECISIONS (decisions.jsonl)"))
    if not decisions:
        lines.append("  (no events recorded for this symbol)")
    else:
        # Show most recent 30
        for d in decisions[-30:]:
            lines.append(_format_decision(d))

    # --- Section: broker calls for this symbol ---
    lines.append(_section("BROKER CALLS (exec_log.jsonl)"))
    if not execs:
        lines.append(
            "  (no broker calls recorded — exec_log only has data since the last code restart)"
        )
    else:
        for e in execs[-20:]:
            lines.append(_format_exec(e))

    # --- Section: IB fills for this symbol ---
    lines.append(_section("IB FILLS (live, last 365 days)"))
    fills = ib.get("fills") or []
    if not fills:
        lines.append("  (no fills recorded by IB in last 30 days)")
    else:
        for f in fills:
            ts = f["time"][:19]
            side = f["side"]
            sh = f["shares"]
            px = f["price"]
            comm = f["commission"]
            pnl = f["realized_pnl"]
            marker = "+" if pnl > 0 else ("-" if pnl < 0 else " ")
            lines.append(
                f"  {ts}  {side:<3} {sh:>4} @ ${px:>8.2f}  "
                f"comm ${comm:>6.2f}  P&L {marker}${abs(pnl):>8.2f}"
            )

    # --- Section: cross-version search (origin of position) ---
    cross = _search_all_versions(symbol)
    if cross:
        lines.append(_section("CROSS-VERSION SEARCH (which bot bought it?)"))
        for vname, hits in cross.items():
            ds = hits["decisions"]
            es = hits["exec_log"]
            lines.append(f"  {vname}/")
            # Find pilot/open/scale_in events specifically — the buy origins
            buys = [d for d in ds if d.get("event") in ("pilot", "open")]
            buys += [e for e in es if e.get("action") in ("open", "broker_rebalance", "scale_in")]
            if buys:
                lines.append(f"    !! BUY EVENTS FOUND in {vname}:")
                for b in buys[:5]:
                    ts = b.get("ts", "")[:19]
                    if "event" in b:
                        lines.append(
                            f"      {ts}  decision: {b.get('event')} "
                            f"reason={b.get('reason', '?')} "
                            f"entry={b.get('entry', '?')} "
                            f"shares={b.get('shares', '?')}"
                        )
                    else:
                        payload = b.get("payload", {}) or {}
                        lines.append(
                            f"      {ts}  exec: {b.get('action')} "
                            f"delta={payload.get('delta', '?')} "
                            f"fill={payload.get('fill_price', '?')}"
                        )
            else:
                lines.append("    only skip/exit events — this bot did NOT buy")
            lines.append(f"    decisions: {len(ds)}  exec_log: {len(es)}")
    else:
        lines.append(_section("CROSS-VERSION SEARCH (which bot bought it?)"))
        lines.append("  no traces of this symbol in any sibling version")
        lines.append("  → most likely: manual order in TWS/Gateway, OR pre-existing")
        lines.append("    paper account position predating any of these bots")

    # --- Section: rule-compliance summary ---
    lines.append(_section("RULE-COMPLIANCE SUMMARY"))
    pilot_evt = next((d for d in decisions if d.get("event") == "pilot"), None)
    if pilot_evt:
        lines.append(f"  Entry trigger: {pilot_evt.get('reason', '?')}")
        breakout = pilot_evt.get("breakout")
        pp = pilot_evt.get("pocket_pivot")
        if breakout is not None:
            lines.append(f"  Was breakout:  {breakout}    Was pocket-pivot: {pp}")
        lines.append(f"  Entry price:   {pilot_evt.get('entry', '?')}")
        lines.append(f"  Initial stop:  {pilot_evt.get('stop', '?')}")
        lines.append(f"  Pilot shares:  {pilot_evt.get('shares', '?')}")
    else:
        lines.append(
            "  (no pilot event recorded for this symbol — likely predates current decisions.jsonl)"
        )

    skips = [d for d in decisions if d.get("event") == "skip"]
    if skips:
        last_skip = skips[-1]
        lines.append(
            f"  Last skip:     {last_skip.get('reason', '?')}    "
            f"failed: {last_skip.get('failed_gates', [])}"
        )

    out = "\n".join(lines)
    if save:
        out_path = ROOT / "state" / f"audit_{symbol}.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out, encoding="utf-8")
        lines.append(f"\n  saved: {out_path.relative_to(ROOT)}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="audit one specific symbol")
    g.add_argument("--all-open", action="store_true", help="audit every open position")
    g.add_argument("--recent", type=int, help="audit the last N closed round trips")
    args = ap.parse_args()

    if args.symbol:
        print(diagnose(args.symbol.upper()))
        return 0

    if args.all_open:
        syms = _find_open_symbols()
        if not syms:
            print("no open positions in state/positions.json")
            return 0
        print(f"auditing {len(syms)} open positions: {', '.join(syms)}")
        for s in syms:
            print(diagnose(s))
        return 0

    if args.recent:
        # Find recently closed symbols from decisions.jsonl
        decisions = _read_jsonl(DECISIONS_PATH)
        exits = [d for d in decisions if d.get("event") == "exit"]
        recent_syms = []
        seen = set()
        for d in reversed(exits):
            s = d.get("symbol")
            if s and s not in seen:
                recent_syms.append(s)
                seen.add(s)
                if len(recent_syms) >= args.recent:
                    break
        if not recent_syms:
            print("no closed round trips found in decisions.jsonl")
            return 0
        print(f"auditing last {len(recent_syms)} closed: {', '.join(recent_syms)}")
        for s in recent_syms:
            print(diagnose(s))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
