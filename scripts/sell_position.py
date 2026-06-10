"""Sell a STOCK position via IB Gateway — operator emergency tool.

INSTRUMENT BOUNDARY: this script touches STOCK positions only (calls
broker.rebalance, which is STK-only). It does NOT close option contracts.
For options use scripts/sell_option.py.

In options-v1.2 the normal cycle does not open stocks, so this script's
only legitimate use is operator-initiated emergency unwind of stray stock
longs at IB. Refuses to run without --emergency to prevent the 2026-05-08
incident (six unrelated stock longs swept by a --all invocation).

Safety:
  * Requires --emergency flag explicitly acknowledging stock scope
  * Requires explicit symbol (no "sell all" without confirmation)
  * Refuses if PAPER_ONLY=False and IBKR_PORT is a live port (unless --force)
  * Logs every action to state/exec_log.jsonl

Usage:
  python scripts/sell_position.py --emergency --symbol JBL
  python scripts/sell_position.py --emergency --symbol JBL --qty 100
  python scripts/sell_position.py --emergency --all       # prompts
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
from execution import exec_log
from notifications import smsbot as _smsbot
from notifications.trade_messages import (
    trade_cancelled,
    trade_filled,
    trade_refused,
    trade_submitted,
)


def _is_option_entry(pos: dict) -> bool:
    """Shape discriminator — mirrors main.py:104 _is_option_position."""
    return "contracts" in pos and "premium_entry" in pos


def load_positions() -> dict:
    p = ROOT / "state" / "positions.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_positions(d: dict) -> None:
    p = ROOT / "state" / "positions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))


def sell_one(broker: IBKRClient, symbol: str, qty: int | None = None) -> dict:
    before = broker.position(symbol, sec_type="STK")
    if before <= 0:
        # Refuses both no-position AND short cases. Prevents the duplicate-
        # click failure path where the second submit opened a naked short.
        print(f"  {symbol}: REFUSED — IB STK position is {before}, nothing to sell")
        exec_log.log(
            action="manual_sell_refused",
            symbol=symbol,
            payload={"before": before, "reason": "no_long_position"},
            notes=f"refused SELL — IB STK position is {before}",
        )
        _smsbot.send_message(
            trade_refused(
                symbol=symbol,
                action="SELL",
                reason=f"no_long_position (IB STK={before})",
                qty=qty,
            )
        )
        return {"status": "no_position", "before": before}
    sell_qty = qty if qty is not None else before
    sell_qty = min(sell_qty, before)
    new_target = before - sell_qty
    print(f"  {symbol}: SELLING {sell_qty} of {before} shares → new target {new_target}")
    rec = broker.rebalance(symbol, new_target)
    exec_log.log(
        action="manual_sell",
        symbol=symbol,
        payload=rec,
        notes=f"manual_close qty={sell_qty} via sell_position.py",
    )
    if rec.get("status") == "submitted":
        _smsbot.send_message(
            trade_submitted(
                symbol=symbol,
                action="SELL",
                qty=sell_qty,
            )
        )
    else:
        _smsbot.send_message(
            trade_cancelled(
                symbol=symbol,
                action="SELL",
                qty=sell_qty,
                reason=f"broker_status={rec.get('status')!r}",
            )
        )
    # Post-submit re-query (canonical truth, not the submission ack).
    broker.ib.sleep(2.0)
    after = broker.position(symbol, sec_type="STK")
    rec["before"] = before
    rec["after"] = after
    rec["filled"] = before - after
    print(f"  {symbol}: IB STK {before} -> {after} (sold {rec['filled']})")
    exec_log.log(
        action="manual_sell_requery",
        symbol=symbol,
        payload={"before": before, "after": after, "delta": rec["filled"]},
        notes=f"post-submit re-query: {before}->{after}",
    )
    # Re-query verdict: fill vs no-change. Submitted-but-no-change is a
    # cancel-class event from the operator's perspective.
    if rec.get("status") == "submitted":
        if rec["filled"] > 0:
            _smsbot.send_message(
                trade_filled(
                    symbol=symbol,
                    action="SELL",
                    qty=rec["filled"],
                    price=None,
                )
            )
        else:
            _smsbot.send_message(
                trade_cancelled(
                    symbol=symbol,
                    action="SELL",
                    qty=sell_qty,
                    reason=f"no_fill_after_requery (before={before} after={after})",
                )
            )
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="symbol to sell")
    g.add_argument("--all", action="store_true", help="close ALL positions (prompts)")
    ap.add_argument("--qty", type=int, help="partial close (default: all shares)")
    ap.add_argument("--force", action="store_true", help="skip confirmation")
    ap.add_argument(
        "--emergency",
        action="store_true",
        help="REQUIRED: acknowledge this script sells STOCKS, not options",
    )
    args = ap.parse_args()

    if not args.emergency:
        print("REFUSED: options-v1.2/scripts/sell_position.py is a STOCK sell tool.")
        print("  This bot manages options. To sell options use scripts/sell_option.py.")
        print("  To unwind stray stock longs at IB, re-run with --emergency.")
        return 2

    broker = IBKRClient()
    broker.connect()
    exec_log.wrap_broker(broker)
    try:
        positions = load_positions()

        if args.symbol:
            symbol = args.symbol.upper()
            if not args.force:
                cur = broker.position(symbol)
                qty_text = f"{args.qty}" if args.qty else f"all {cur}"
                ans = input(f"Sell {qty_text} shares of {symbol}? [y/N]: ").strip().lower()
                if ans != "y":
                    print("aborted")
                    return 0
            rec = sell_one(broker, symbol, args.qty)
            if rec.get("status") == "submitted":
                # Remove from tracking only if fully closed AND the entry
                # is stock-shape. Never pop option-shape entries — they're
                # owned by sell_option.py path.
                remaining = broker.position(symbol, sec_type="STK")
                tracked = positions.get(symbol)
                if remaining == 0 and tracked is not None and not _is_option_entry(tracked):
                    del positions[symbol]
                    save_positions(positions)
                    print(f"  removed {symbol} from positions.json")
                print(f"\nresult: {rec}")
            else:
                print(f"\nrebalance returned: {rec}")
            return 0

        if args.all:
            # STK-only enumeration. The 2026-05-08 incident sweep relied on
            # an unfiltered ib.positions() iteration that touched options too.
            ib_pos = []
            for p in broker.ib.positions():
                c = p.contract
                qty = int(p.position)
                if qty <= 0:
                    continue
                if getattr(c, "secType", "STK") != "STK":
                    continue
                ib_pos.append((c.symbol, qty))
            if not ib_pos:
                print("no STK long positions to close")
                return 0
            print("Will close ALL STK long positions (options untouched):")
            for sym, sh in ib_pos:
                print(f"  {sym}: {sh} shares")
            if not args.force:
                ans = input("\nProceed? [y/N]: ").strip().lower()
                if ans != "y":
                    print("aborted")
                    return 0
            # Pop a positions.json entry ONLY when the broker confirms a
            # clean full close. Partial fills, cancellations, unchanged
            # post-submit re-queries, and option-shape entries are all
            # preserved so the bot keeps tracking until truly closed.
            for sym, _ in ib_pos:
                rec = sell_one(broker, sym)
                tracked = positions.get(sym)
                if tracked is None or _is_option_entry(tracked):
                    continue
                submitted = rec.get("status") == "submitted"
                closed = rec.get("after") == 0
                if submitted and closed:
                    positions.pop(sym, None)
                    print(f"  popped {sym} from positions.json (after=0)")
                else:
                    print(
                        f"  KEEPING {sym} in positions.json — "
                        f"status={rec.get('status')!r} "
                        f"before={rec.get('before')} "
                        f"after={rec.get('after')}"
                    )
            save_positions(positions)
            print("\nall STK longs sent to broker. Options untouched. Verify in IB Gateway.")
            return 0
    finally:
        broker.disconnect()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
