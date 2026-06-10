"""Reconcile state/positions.json against IB-held option contracts.

Drops any option entry whose strike+expiry+symbol+right doesn't match
an actual IB position with qty > 0. Stock positions are left alone.

This is the cleanup tool for the phantom-positions class of bug — when
the bot's state file claims an option position that IB never actually
filled (or that's since been closed externally). Without it, the bot's
manage_existing might fire SELL orders against contracts we don't own,
which IB accepts as opening a NAKED SHORT.

Originally lived inline in run.sh as a heredoc. Extracted to a real
script so the menu's `pause` after invocation isn't broken by stdin
left in a weird state by the heredoc.

Usage:
  python scripts/clear_phantom_options.py
  python scripts/clear_phantom_options.py --dry-run    # show what'd be dropped, don't write
"""

from __future__ import annotations

import argparse
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

from broker.ibkr_client import IBKRClient

POS_FILE = ROOT / "state" / "positions.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be dropped, don't write positions.json",
    )
    args = ap.parse_args()

    if not POS_FILE.exists():
        print(f"  no {POS_FILE.relative_to(ROOT)} — nothing to do")
        return 0
    try:
        positions = json.loads(POS_FILE.read_text() or "{}")
    except json.JSONDecodeError:
        print(f"  ! {POS_FILE.relative_to(ROOT)} is invalid JSON — refusing to overwrite")
        return 1
    if not positions:
        print("  positions.json empty — nothing to do")
        return 0

    broker = IBKRClient()
    broker.connect()
    held = []
    try:
        for p in broker.ib.positions():
            c = p.contract
            qty = int(round(float(p.position)))
            if getattr(c, "secType", "") == "OPT" and qty > 0:
                held.append(
                    {
                        "symbol": c.symbol,
                        "strike": float(c.strike),
                        "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
                        "right": getattr(c, "right", "C"),
                        "qty": qty,
                    }
                )
    finally:
        broker.disconnect()

    print(f"  IB holds {len(held)} option contract(s)")
    for h in held:
        print(f"    {h['symbol']:<6} {h['qty']}x ${h['strike']}{h['right']} {h['expiry']}")

    dropped: list[tuple] = []
    kept: dict = {}
    for sym, pos in positions.items():
        # Stock positions left untouched.
        if "contracts" not in pos:
            kept[sym] = pos
            continue
        expiry = pos.get("expiry", "")
        strike = float(pos.get("strike", 0))
        right = pos.get("right", "C")
        matched = any(
            h["symbol"] == sym
            and abs(h["strike"] - strike) < 0.01
            and h["expiry"] == expiry
            and h["right"] == right
            for h in held
        )
        if matched:
            kept[sym] = pos
        else:
            dropped.append((sym, strike, expiry, right, pos.get("contracts")))

    if not dropped:
        print(f"  all {len(positions)} positions verified — no changes")
        return 0

    print(f"\n  PHANTOM POSITIONS to drop ({len(dropped)}):")
    for sym, strike, expiry, right, n in dropped:
        print(f"    {sym:<6} {n}x ${strike}{right} {expiry}")
    print(f"  kept after drop: {len(kept)} verified positions")

    if args.dry_run:
        print("\n  [DRY-RUN] no changes written")
        return 0

    POS_FILE.write_text(json.dumps(kept, indent=2))
    print(f"\n  wrote {POS_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
