"""Cover (buy back) a short STK or OPT position via IB.

Created after the 2026-05-08 naked-short incident:
  - AAPL/AMSC STOCK shorts opened by dashboard Sell clicks on rows that
    looked like stock but were option holdings.
  - ADI/ALB OPTION shorts opened by dashboard double-clicks on Untracked
    IB options Sell button (1 -> 0 -> -1 from successive submissions).

The new long-only guards in broker.rebalance() and broker.place_option_order()
prevent NEW shorts; this script unwinds existing ones for both types.

Usage:
  python scripts/cover_short.py --symbol AAPL              # cover stock short
  python scripts/cover_short.py --symbol AAPL --qty 3      # partial cover
  python scripts/cover_short.py --all                      # cover every short (STK + OPT)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Unique clientId per script invocation. PID-based: each subprocess gets
# a stable id derived from its PID. Avoids Error 326 collision with the
# running bot AND collisions between concurrent ad-hoc scripts.
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


def cover_one(broker: IBKRClient, symbol: str, qty: int | None = None) -> dict:
    before = broker.position(symbol, sec_type="STK")
    if before >= 0:
        print(f"  {symbol}: not short (current STK={before}) — nothing to cover")
        _smsbot.send_message(
            trade_refused(
                symbol=symbol,
                action="BUY",
                reason=f"not_short (IB STK={before})",
                qty=qty,
            )
        )
        return {"status": "not_short", "before": before}
    short_size = -before  # before is negative; e.g. -5 → short_size=5
    cover_qty = qty if qty is not None else short_size
    cover_qty = min(cover_qty, short_size)
    new_target = before + cover_qty  # toward zero
    print(
        f"  {symbol}: COVERING {cover_qty} of {short_size} short shares "
        f"(current={before} → target={new_target})"
    )
    rec = broker.rebalance(symbol, new_target)
    exec_log.log(
        action="manual_cover_short",
        symbol=symbol,
        payload=rec,
        notes=f"cover {cover_qty} of {short_size} short shares",
    )
    if rec.get("status") == "submitted":
        _smsbot.send_message(
            trade_submitted(
                symbol=symbol,
                action="BUY",
                qty=cover_qty,
            )
        )
    else:
        _smsbot.send_message(
            trade_cancelled(
                symbol=symbol,
                action="BUY",
                qty=cover_qty,
                reason=f"broker_status={rec.get('status')!r}",
            )
        )
    # Post-submit re-query — truth is what IB holds now, not the ack.
    broker.ib.sleep(2.0)
    after = broker.position(symbol, sec_type="STK")
    rec["before"] = before
    rec["after"] = after
    rec["covered"] = after - before
    print(f"  {symbol}: IB STK {before} -> {after} (covered {rec['covered']})")
    exec_log.log(
        action="manual_cover_short_requery",
        symbol=symbol,
        payload={"before": before, "after": after, "delta": rec["covered"]},
        notes=f"post-submit re-query: {before}->{after}",
    )
    if rec.get("status") == "submitted":
        if rec["covered"] > 0:
            _smsbot.send_message(
                trade_filled(
                    symbol=symbol,
                    action="BUY",
                    qty=rec["covered"],
                    price=None,
                )
            )
        else:
            _smsbot.send_message(
                trade_cancelled(
                    symbol=symbol,
                    action="BUY",
                    qty=cover_qty,
                    reason=f"no_fill_after_requery (before={before} after={after})",
                )
            )
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="symbol to cover")
    g.add_argument("--all", action="store_true", help="cover ALL short stock positions (prompts)")
    ap.add_argument("--qty", type=int, help="partial cover (default: full)")
    ap.add_argument("--force", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    broker = IBKRClient()
    broker.connect()
    exec_log.wrap_broker(broker)
    try:
        if args.symbol:
            sym = args.symbol.upper()
            cur = broker.position(sym, sec_type="STK")
            if cur >= 0:
                print(f"  {sym}: STK position is {cur} (not short) — nothing to do")
                return 0
            if not args.force:
                qty_text = f"{args.qty}" if args.qty else f"all {-cur}"
                ans = (
                    input(f"BUY {qty_text} shares of {sym} to cover short? [y/N]: ").strip().lower()
                )
                if ans != "y":
                    print("aborted")
                    return 0
            rec = cover_one(broker, sym, args.qty)
            print(f"\nresult: {rec}")
            return 0

        if args.all:
            stk_shorts = []  # [(sym, qty)]
            opt_shorts = []  # [(sym, expiry, strike, right, qty)]
            for p in broker.ib.positions():
                c = p.contract
                qty = int(round(float(p.position)))
                if qty >= 0:
                    continue
                sec_type = getattr(c, "secType", "STK")
                if sec_type == "STK":
                    stk_shorts.append((c.symbol, qty))
                elif sec_type == "OPT":
                    opt_shorts.append(
                        (
                            c.symbol,
                            getattr(c, "lastTradeDateOrContractMonth", ""),
                            float(getattr(c, "strike", 0) or 0),
                            getattr(c, "right", "C"),
                            qty,
                        )
                    )
            if not stk_shorts and not opt_shorts:
                print("no short positions to cover")
                return 0
            print(f"STK shorts: {len(stk_shorts)}, OPT shorts: {len(opt_shorts)}")
            for sym, q in stk_shorts:
                print(f"  STK {sym}: {q} shares (cover by buying {-q})")
            for sym, exp, strike, right, q in opt_shorts:
                print(f"  OPT {sym} ${strike}{right} {exp}: {q} contracts (cover by buying {-q})")
            if not args.force:
                ans = input("\nProceed? [y/N]: ").strip().lower()
                if ans != "y":
                    print("aborted")
                    return 0
            for sym, _ in stk_shorts:
                cover_one(broker, sym)
            for sym, exp, strike, right, qty in opt_shorts:
                buy_qty = -qty
                print(f"  COVERING OPT {sym} ${strike}{right} {exp}: BUY {buy_qty} (MKT)")
                # MKT here on purpose — covering is a safety op; pay the
                # spread for guaranteed fill rather than risk a LMT-at-mid
                # that doesn't fill while exposure stays open.
                rec = broker.place_option_order(
                    symbol=sym,
                    expiry=exp,
                    strike=strike,
                    right=right,
                    action="BUY",
                    contracts=buy_qty,
                    order_type="MKT",
                )
                exec_log.log(
                    action="manual_cover_option_short",
                    symbol=sym,
                    payload=rec,
                    notes=f"cover OPT short {qty} via BUY {buy_qty} MKT ${strike}{right} {exp}",
                )
                if rec.get("status") == "submitted":
                    _smsbot.send_message(
                        trade_submitted(
                            symbol=sym,
                            action="BUY",
                            qty=buy_qty,
                            expiry=exp,
                            strike=strike,
                            right=right,
                            order_type="MKT",
                        )
                    )
                else:
                    _smsbot.send_message(
                        trade_cancelled(
                            symbol=sym,
                            action="BUY",
                            qty=buy_qty,
                            expiry=exp,
                            strike=strike,
                            right=right,
                            reason=f"broker_status={rec.get('status')!r}",
                        )
                    )
                # Post-submit re-query, full instrument identity.
                broker.ib.sleep(2.0)
                after_q = broker._option_position_qty(sym, exp, strike, right)
                print(f"  OPT {sym} ${strike}{right} {exp}: {qty} -> {after_q}")
                exec_log.log(
                    action="manual_cover_option_short_requery",
                    symbol=sym,
                    payload={
                        "before": qty,
                        "after": after_q,
                        "delta": after_q - qty,
                        "strike": strike,
                        "expiry": exp,
                        "right": right,
                    },
                    notes=f"post-submit re-query: {qty}->{after_q}",
                )
                if rec.get("status") == "submitted":
                    delta = after_q - qty
                    if delta > 0:
                        _smsbot.send_message(
                            trade_filled(
                                symbol=sym,
                                action="BUY",
                                qty=delta,
                                price=None,
                                expiry=exp,
                                strike=strike,
                                right=right,
                            )
                        )
                    else:
                        _smsbot.send_message(
                            trade_cancelled(
                                symbol=sym,
                                action="BUY",
                                qty=buy_qty,
                                expiry=exp,
                                strike=strike,
                                right=right,
                                reason=f"no_fill_after_requery (before={qty} after={after_q})",
                            )
                        )
            print("\nall shorts sent to broker. Verify in IB Gateway.")
            return 0
    finally:
        broker.disconnect()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
