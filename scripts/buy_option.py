"""Manual options BUY via IB — uses MID order type (LMT at NBBO midpoint).
IB's MIDPRICE algo is rejected on options/SMART with Error 387, so we
fetch bid/ask and place an explicit LMT at (bid+ask)/2.

Buys N contracts of an option. If strike/expiry not specified, auto-picks
~10-15% OTM with 90-180 DTE per options-v1.2 strategy spec.

Usage:
  python scripts/buy_option.py --symbol AAPL --contracts 10                 # auto-pick
  python scripts/buy_option.py --symbol AAPL --contracts 10 --strike 240 --expiry 20260920
  python scripts/buy_option.py --symbol AAPL --contracts 10 --right P       # put instead of call
  python scripts/buy_option.py --symbol AAPL --contracts 10 --order-type LMT --limit-price 5.50
"""

from __future__ import annotations

import argparse
import datetime as dt
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


def auto_pick_contract(
    broker: IBKRClient, symbol: str, right: str = "C", otm_pct: float = 12.5, dte_target: int = 120
) -> tuple[str, float]:
    """Pick an OTM contract ~12.5% OTM, ~120 days out."""
    # Get current price
    try:
        px = broker.market_price(symbol)
    except Exception as e:
        print(f"buy_option_market_price_failed: {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
        px = 0.0
    if px <= 0:
        import yfinance as yf

        try:
            px = float(yf.Ticker(symbol).history(period="2d", interval="1d")["Close"].iloc[-1])
        except Exception as e:
            print(
                f"buy_option_yfinance_fallback_failed: {symbol}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            raise RuntimeError(f"could not get price for {symbol}") from e

    # Strike: round to nearest $5 increment for liquidity
    target_strike = px * (1 + otm_pct / 100) if right == "C" else px * (1 - otm_pct / 100)
    if px < 50:
        strike_inc = 1
    elif px < 200:
        strike_inc = 5
    else:
        strike_inc = 10
    strike = round(target_strike / strike_inc) * strike_inc

    # Expiry: pick the nearest standard monthly expiry to dte_target
    target_date = dt.date.today() + dt.timedelta(days=dte_target)
    # Find third Friday of target month
    year, month = target_date.year, target_date.month
    first_day = dt.date(year, month, 1)
    days_to_first_fri = (4 - first_day.weekday()) % 7
    third_fri = first_day + dt.timedelta(days=days_to_first_fri + 14)
    expiry = third_fri.strftime("%Y%m%d")

    print(
        f"  auto-pick: stock @ ${px:.2f}, {right} strike ${strike} exp {expiry} ({dte_target}d DTE)"
    )
    return expiry, strike


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--contracts", type=int, required=True)
    ap.add_argument("--strike", type=float, help="strike price (auto if omitted)")
    ap.add_argument("--expiry", help="YYYYMMDD (auto if omitted)")
    ap.add_argument("--right", default="C", choices=["C", "P"])
    ap.add_argument("--order-type", default="MID", choices=["MID", "LMT", "MKT"])
    ap.add_argument("--limit-price", type=float, help="required for LMT")
    ap.add_argument("--otm-pct", type=float, default=12.5, help="auto-pick OTM%% (default 12.5)")
    ap.add_argument("--dte", type=int, default=120, help="auto-pick DTE target (default 120)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sym = args.symbol.upper()

    broker = IBKRClient()
    broker.connect()
    exec_log.wrap_broker(broker)
    try:
        if args.strike and args.expiry:
            strike, expiry = args.strike, args.expiry
        else:
            expiry, strike = auto_pick_contract(broker, sym, args.right, args.otm_pct, args.dte)

        if not args.force:
            print("\nABOUT TO PLACE OPTION ORDER:")
            print(f"  {args.contracts}x {sym} {expiry} ${strike} {args.right}")
            print(
                f"  order type: {args.order_type}"
                + (f" @ ${args.limit_price}" if args.order_type == "LMT" else "")
            )
            ans = input("\nProceed? [y/N]: ").strip().lower()
            if ans != "y":
                print("aborted")
                return 0

        rec = broker.place_option_order(
            symbol=sym,
            expiry=expiry,
            strike=strike,
            right=args.right,
            action="BUY",
            contracts=args.contracts,
            order_type=args.order_type,
            limit_price=args.limit_price,
        )
        exec_log.log(
            action="manual_buy_option",
            symbol=sym,
            payload=rec,
            notes=f"manual_option_buy {args.contracts}x ${strike}{args.right} {expiry}",
        )
        print(f"\nresult: {rec}")
    finally:
        broker.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
