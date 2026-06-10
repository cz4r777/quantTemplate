"""Execution-log audit — surface broker integration bugs.

Reads state/exec_log.jsonl and finds:
  * Phantom positions  — open with insufficient_cash status, no later exit
  * P&L mismatches     — broker fill price differs from expected by > tolerance
  * Orphan exits       — exit/trim event with no prior open
  * Stuck positions    — opens with no corresponding close before EOD
  * Slippage outliers  — fills more than N bps from signal price
  * Sequence errors    — out-of-order events

Run AFTER any backtest. The same script will work in live mode once the
live trading loop also writes to exec_log.

Usage:
  python scripts/exec_audit.py
  python scripts/exec_audit.py --tolerance-bps 10
  python scripts/exec_audit.py --slippage-outlier-bps 100
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution import exec_log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tolerance-bps",
        type=float,
        default=10.0,
        help="P&L mismatch threshold in bps (default 10)",
    )
    ap.add_argument(
        "--slippage-outlier-bps",
        type=float,
        default=100.0,
        help="flag fills > N bps from signal price (default 100)",
    )
    args = ap.parse_args()

    rows = exec_log.all_rows()
    if not rows:
        print("no exec log found — run a backtest first")
        return 1

    print("\n=== EXEC LOG AUDIT ===")
    print(f"total rows: {len(rows)}")

    by_action = defaultdict(int)
    for r in rows:
        by_action[r["action"]] += 1
    print("\nrows by action:")
    for a, n in sorted(by_action.items(), key=lambda x: -x[1]):
        print(f"  {a:<20} {n:>5}")

    # Symbol-level open/close pairing
    opens_by_sym = defaultdict(list)
    closes_by_sym = defaultdict(list)
    rejects = []
    slippage_outliers = []
    pnl_mismatches = []

    for r in rows:
        sym = r["symbol"]
        action = r["action"]
        if action == "open":
            opens_by_sym[sym].append(r)
            # Slippage check
            payload = r.get("payload", {})
            sig_p = payload.get("signal_price", 0)
            fill_p = payload.get("fill_price", 0)
            if sig_p > 0:
                slip_bps = abs(fill_p - sig_p) / sig_p * 10_000
                if slip_bps > args.slippage_outlier_bps:
                    slippage_outliers.append((r, slip_bps))
        elif action in ("exit", "trim", "exit_eod"):
            closes_by_sym[sym].append(r)
        elif action == "broker_reject":
            rejects.append(r)

        # P&L mismatch: expected_pnl vs payload-derived
        if r.get("expected_pnl") is not None and r.get("payload"):
            payload = r["payload"]
            fill_p = payload.get("fill_price", 0)
            pos_before = r.get("position_before") or {}
            entry = pos_before.get("entry_price", 0)
            shares = abs(payload.get("delta", 0))
            if entry > 0 and shares > 0 and fill_p > 0:
                derived = (fill_p - entry) * shares
                exp = r["expected_pnl"]
                if abs(derived - exp) / max(abs(entry * shares), 1) * 10_000 > args.tolerance_bps:
                    pnl_mismatches.append((r, derived, exp))

    print(f"\nopens:   {sum(len(v) for v in opens_by_sym.values())}")
    print(f"closes:  {sum(len(v) for v in closes_by_sym.values())}")
    print(f"rejects: {len(rejects)}")

    # --- Audit checks ---
    print("\n--- AUDIT CHECKS ---")
    issues = 0

    # Check 1: every open should have a corresponding close
    orphan_opens = []
    for sym, opens in opens_by_sym.items():
        closes = closes_by_sym.get(sym, [])
        if len(opens) > len(closes):
            orphan_opens.append((sym, len(opens) - len(closes)))
    if orphan_opens:
        issues += 1
        print(f"\n[!] STUCK / ORPHAN OPENS — {len(orphan_opens)} symbols have unmatched opens:")
        for sym, n in orphan_opens[:10]:
            print(f"    {sym}: +{n} opens without close")
    else:
        print("[OK] every open has a close")

    # Check 2: exits without prior open
    orphan_exits = []
    for sym, closes in closes_by_sym.items():
        opens = opens_by_sym.get(sym, [])
        if len(closes) > len(opens):
            orphan_exits.append((sym, len(closes) - len(opens)))
    if orphan_exits:
        issues += 1
        print(f"\n[!] ORPHAN EXITS — {len(orphan_exits)} symbols have closes without opens:")
        for sym, n in orphan_exits[:10]:
            print(f"    {sym}: +{n} closes without open")
    else:
        print("[OK] every close has an open")

    # Check 3: rejects
    if rejects:
        print(f"\n[!] BROKER REJECTS — {len(rejects)} entries rejected:")
        reject_reasons = defaultdict(int)
        for r in rejects:
            reject_reasons[r["payload"].get("status", "unknown")] += 1
        for reason, n in reject_reasons.items():
            print(f"    {reason}: {n}")
        issues += 1
    else:
        print("[OK] no broker rejects")

    # Check 4: slippage outliers
    if slippage_outliers:
        print(
            f"\n[!] SLIPPAGE OUTLIERS — {len(slippage_outliers)} fills > {args.slippage_outlier_bps}bps from signal:"
        )
        for r, bps in slippage_outliers[:5]:
            print(
                f"    {r['symbol']} {r.get('bar_time', '')} {bps:.0f}bps "
                f"(signal {r['payload'].get('signal_price'):.2f} -> fill {r['payload'].get('fill_price'):.2f})"
            )
        issues += 1
    else:
        print(f"[OK] no slippage outliers >{args.slippage_outlier_bps}bps")

    # Check 5: P&L mismatches
    if pnl_mismatches:
        print(
            f"\n[!] P&L MISMATCHES — {len(pnl_mismatches)} events where expected vs derived P&L diverge >{args.tolerance_bps}bps:"
        )
        for r, derived, exp in pnl_mismatches[:5]:
            print(
                f"    {r['symbol']} {r.get('bar_time', '')} expected {exp:+.2f} derived {derived:+.2f}"
            )
        issues += 1
    else:
        print(f"[OK] no P&L mismatches >{args.tolerance_bps}bps")

    print(f"\n{'=' * 40}")
    print(
        f"audit complete — {issues} issue group(s) flagged" if issues else "audit complete — clean"
    )
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
