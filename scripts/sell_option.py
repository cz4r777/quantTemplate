"""Manual option SELL via IB — closes a specific contract.

Used by the dashboard's "Sell" button on the Untracked IB options panel
to unwind option contracts that aren't managed by the bot (e.g. stress
demo positions held in IB but not in any bot's positions.json).

Usage:
  python scripts/sell_option.py --symbol AAPL --strike 290 --expiry 20260515
  python scripts/sell_option.py --symbol SPY --strike 580 --expiry 20260620 --contracts 5
  python scripts/sell_option.py --symbol QQQ --strike 470 --expiry 20260620 --right C --order-type LMT --limit-price 0.50

Defaults:
  --right         C
  --contracts     1 (single contract)
  --order-type    MID (LMT at NBBO midpoint, default). MIDPRICE is rejected
                  on options/SMART (Error 387), so this places an explicit
                  LMT at (bid+ask)/2.
"""

from __future__ import annotations

import argparse
import sys
import time
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


def _order_status(broker: IBKRClient, order_id: int | None) -> str:
    if order_id is None:
        return "Unknown"
    try:
        for trade in broker.ib.trades():
            if getattr(trade.order, "orderId", None) == order_id:
                return trade.orderStatus.status or "Unknown"
    except Exception as e:
        print(
            f"sell_option_order_status_query_failed: order_id={order_id}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
    return "Unknown"


def _poll_position_change(
    broker: IBKRClient,
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
    before: int,
    order_id: int | None,
    wait_secs: float,
    poll_secs: float,
) -> tuple[int, int, str, list[dict]]:
    deadline = time.monotonic() + wait_secs
    terminal = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
    observations: list[dict] = []

    while True:
        broker.ib.sleep(poll_secs)
        after = broker._option_position_qty(symbol, expiry, strike, right)
        status = _order_status(broker, order_id)
        delta = before - after
        observations.append({"after": after, "delta": delta, "ib_status": status})
        if delta > 0:
            return after, delta, status, observations
        if status in terminal:
            return after, delta, status, observations
        if time.monotonic() >= deadline:
            return after, delta, status, observations


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--strike", type=float, required=True)
    ap.add_argument("--expiry", required=True, help="YYYYMMDD")
    ap.add_argument("--right", default="C", choices=["C", "P"])
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--order-type", default="MID", choices=["MID", "LMT", "MKT"])
    ap.add_argument("--limit-price", type=float, help="required for LMT")
    ap.add_argument(
        "--wait-secs",
        type=float,
        default=30.0,
        help="poll IB position/order status before declaring no-change",
    )
    ap.add_argument(
        "--poll-secs", type=float, default=2.0, help="seconds between post-submit re-query attempts"
    )
    ap.add_argument("--force", action="store_true", help="skip confirmation (used by dashboard)")
    args = ap.parse_args()

    sym = args.symbol.upper()

    if not args.force:
        print("\nABOUT TO SELL OPTION:")
        print(f"  {args.contracts}x {sym} {args.expiry} ${args.strike} {args.right}")
        print(
            f"  order type: {args.order_type}"
            + (f" @ ${args.limit_price}" if args.order_type == "LMT" else "")
        )
        ans = input("\nProceed? [y/N]: ").strip().lower()
        if ans != "y":
            print("aborted")
            return 0

    broker = IBKRClient()
    broker.connect()
    exec_log.wrap_broker(broker)
    try:
        before = broker._option_position_qty(sym, args.expiry, args.strike, args.right)
        if before <= 0:
            # Refuse: nothing to sell. Prevents the 2026-05-08 double-click
            # naked-short path where second click after fill opened a short.
            print(
                f"REFUSED: no long position to sell. IB shows "
                f"{sym} ${args.strike}{args.right} {args.expiry} = {before}."
            )
            exec_log.log(
                action="manual_sell_option_refused",
                symbol=sym,
                payload={"reason": "no_position", "before": before},
                notes=f"refused SELL — IB position is {before} contracts",
            )
            _smsbot.send_message(
                trade_refused(
                    symbol=sym,
                    action="SELL",
                    reason=f"no_long_position (IB OPT={before})",
                    expiry=args.expiry,
                    strike=args.strike,
                    right=args.right,
                    qty=args.contracts,
                )
            )
            return 3
        if before < args.contracts:
            print(
                f"REFUSED: requested {args.contracts} > held {before}. "
                f"Re-run with --contracts {before} to close the lot."
            )
            _smsbot.send_message(
                trade_refused(
                    symbol=sym,
                    action="SELL",
                    reason=f"requested {args.contracts} > held {before}",
                    expiry=args.expiry,
                    strike=args.strike,
                    right=args.right,
                    qty=args.contracts,
                )
            )
            return 3

        rec = broker.place_option_order(
            symbol=sym,
            expiry=args.expiry,
            strike=args.strike,
            right=args.right,
            action="SELL",
            contracts=args.contracts,
            order_type=args.order_type,
            limit_price=args.limit_price,
        )
        exec_log.log(
            action="manual_sell_option",
            symbol=sym,
            payload=rec,
            notes=f"manual SELL {args.contracts}x ${args.strike}{args.right} {args.expiry}",
        )
        if rec.get("status") == "cancelled":
            print(f"\nCANCELLED by IB: {rec.get('error', 'unknown')}")
            _smsbot.send_message(
                trade_cancelled(
                    symbol=sym,
                    action="SELL",
                    reason=str(rec.get("error", "unknown")),
                    expiry=args.expiry,
                    strike=args.strike,
                    right=args.right,
                    qty=args.contracts,
                )
            )
            return 1
        _smsbot.send_message(
            trade_submitted(
                symbol=sym,
                action="SELL",
                qty=args.contracts,
                expiry=args.expiry,
                strike=args.strike,
                right=args.right,
                order_type=args.order_type,
                limit_price=args.limit_price,
            )
        )

        # Post-submit re-query. IB option MID orders often fill 5-30s after
        # submission; a single 2s check produced false dashboard failures.
        order_id = rec.get("order_id")
        after, delta, final_status, observations = _poll_position_change(
            broker,
            sym,
            args.expiry,
            args.strike,
            args.right,
            before,
            order_id,
            args.wait_secs,
            args.poll_secs,
        )
        print(f"\nresult: {rec}")
        print(
            f"IB position {sym} ${args.strike}{args.right} {args.expiry}: "
            f"{before} -> {after} (sold {delta})"
        )
        exec_log.log(
            action="manual_sell_option_requery",
            symbol=sym,
            payload={
                "before": before,
                "after": after,
                "delta": delta,
                "final_ib_status": final_status,
                "polls": observations,
            },
            notes=f"post-submit poll: {before}->{after} status={final_status}",
        )
        if after == before:
            print(
                f"WARNING: position unchanged after {args.wait_secs:.0f}s. "
                f"Final IB status={final_status}. Check IB before retrying."
            )
            _smsbot.send_message(
                trade_cancelled(
                    symbol=sym,
                    action="SELL",
                    reason=f"no_fill_after_{args.wait_secs:.0f}s (status={final_status})",
                    expiry=args.expiry,
                    strike=args.strike,
                    right=args.right,
                    qty=args.contracts,
                )
            )
            return 2
        _smsbot.send_message(
            trade_filled(
                symbol=sym,
                action="SELL",
                qty=delta,
                price=None,
                expiry=args.expiry,
                strike=args.strike,
                right=args.right,
            )
        )
        return 0
    finally:
        broker.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
