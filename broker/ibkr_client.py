from typing import Any

from ib_insync import IB, LimitOrder, MarketOrder, Option, Stock

from config import IBKR_CLIENT_ID, IBKR_HOST, IBKR_PORT


class IBKRClient:
    def __init__(self):
        self.ib = IB()
        self._requested_live_market_data = False

    def connect(self, timeout: float = 10.0) -> None:
        if not self.ib.isConnected():
            # Explicit timeout prevents hangs if Gateway is unresponsive
            self.ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=timeout)
            # Prefer live data (type 1). IB falls back to delayed (3)
            # automatically for instruments we don't have subscriptions for.
            #   1 = live, 2 = frozen, 3 = delayed, 4 = delayed-frozen
            try:
                self.ib.reqMarketDataType(1)
                self._requested_live_market_data = True
            except Exception:
                self._requested_live_market_data = False

    def reconcile(self, tracked_positions: dict) -> dict:
        """Compare state/positions.json against broker positions, keyed by
        FULL INSTRUMENT IDENTITY (symbol + secType + strike + expiry + right
        for options; symbol + STK for stocks). Symbol-only matching caused
        the 2026-05-08 phantom failure: bot held AAPL OPT, IB held AAPL STK,
        symbol-keyed reconcile treated them as the same position.

        Returns a report of discrepancies; does NOT auto-fix (too risky)."""

        def _key_stk(sym: str) -> tuple:
            return (sym, "STK", 0.0, "", "")

        def _key_opt(sym: str, strike: float, expiry: str, right: str) -> tuple:
            return (sym, "OPT", round(float(strike or 0), 4), expiry or "", right or "")

        def _tracked_key(sym: str, pos: dict) -> tuple:
            # Options carry contracts + premium_entry; stocks carry shares.
            # Reuses the same shape discriminator as main.py _is_option_position.
            if "contracts" in pos and "premium_entry" in pos:
                return _key_opt(
                    sym, pos.get("strike", 0), pos.get("expiry", ""), pos.get("right", "C")
                )
            return _key_stk(sym)

        def _tracked_qty(pos: dict) -> int:
            if "contracts" in pos and "premium_entry" in pos:
                return int(pos.get("contracts", 0) or 0)
            return int(pos.get("shares", 0) or 0)

        broker_positions: dict[tuple, int] = {}
        for p in self.ib.positions():
            c = p.contract
            sec = getattr(c, "secType", "STK")
            if sec == "OPT":
                key = _key_opt(
                    c.symbol,
                    float(getattr(c, "strike", 0) or 0),
                    getattr(c, "lastTradeDateOrContractMonth", ""),
                    getattr(c, "right", "C"),
                )
            else:
                key = _key_stk(c.symbol)
            broker_positions[key] = int(p.position)

        report: dict[str, list[dict[str, Any]]] = {
            "divergences": [],
            "orphans": [],
            "untracked": [],
        }
        seen: set[tuple] = set()
        for sym, pos in tracked_positions.items():
            key = _tracked_key(sym, pos)
            seen.add(key)
            tracked = _tracked_qty(pos)
            actual = broker_positions.get(key, 0)
            if tracked != actual:
                row = {
                    "symbol": sym,
                    "sec_type": key[1],
                    "strike": key[2],
                    "expiry": key[3],
                    "right": key[4],
                    "tracked": tracked,
                    "actual": actual,
                }
                if actual == 0:
                    report["orphans"].append(row)
                else:
                    report["divergences"].append(row)
        for key, actual in broker_positions.items():
            if key in seen or actual == 0:
                continue
            report["untracked"].append(
                {
                    "symbol": key[0],
                    "sec_type": key[1],
                    "strike": key[2],
                    "expiry": key[3],
                    "right": key[4],
                    "actual": actual,
                }
            )
        return report

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def equity(self) -> float:
        for row in self.ib.accountSummary():
            if row.tag == "NetLiquidation":
                return float(row.value)
        return 0.0

    def position(self, symbol: str, sec_type: str = "STK") -> int:
        """Net position in `symbol` filtered by `sec_type`. Defaults to STK
        so callers asking 'how many shares of AAPL' don't accidentally sum
        in option contracts. Pass sec_type='OPT' to query contract count."""
        for p in self.ib.positions():
            c = p.contract
            if c.symbol == symbol and getattr(c, "secType", "STK") == sec_type:
                return int(p.position)
        return 0

    def _option_position_qty(self, symbol: str, expiry: str, strike: float, right: str) -> int:
        """Return net contracts held for the EXACT option (sym+expiry+strike+right).
        Used by the naked-short guard in place_option_order."""
        for p in self.ib.positions():
            c = p.contract
            if (
                getattr(c, "secType", "") == "OPT"
                and c.symbol == symbol
                and abs(float(getattr(c, "strike", 0) or 0) - float(strike)) < 0.01
                and getattr(c, "lastTradeDateOrContractMonth", "") == expiry
                and getattr(c, "right", "") == right
            ):
                return int(p.position)
        return 0

    def _stock(self, symbol: str) -> Stock:
        c = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(c)
        return c

    def market_price(self, symbol: str) -> float:
        contract = self._stock(symbol)
        ticker = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(1)
        price = ticker.marketPrice()
        return float(price) if price and price == price else 0.0

    def list_option_strikes(self, symbol: str, expiry: str) -> list[float]:
        """Return sorted listed call strikes for (symbol, yyyymmdd_expiry).

        Thin wrapper around IB's reqSecDefOptParams used by the
        T-OPT-STRIKEGRID2 chain-truth path. Read-only — never places an
        order, never mutates state. Returns [] on any failure so the
        selector's fail-closed branch kicks in cleanly (skip the signal
        rather than re-emit a static-grid invalid strike).

        Failure modes that collapse to []:
          - stock symbol fails to qualify (conId=0)
          - reqSecDefOptParams raises / times out
          - no chain returned for this underlying
          - the given expiry isn't listed on any returned chain
        """
        try:
            stock = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(stock)
            con_id = getattr(stock, "conId", 0) or 0
            if not con_id:
                return []
            chains = self.ib.reqSecDefOptParams(symbol, "", "STK", con_id)
            if not chains:
                return []
            strikes: set[float] = set()
            for ch in chains:
                expirations = getattr(ch, "expirations", None) or set()
                if expiry not in expirations:
                    continue
                for s in getattr(ch, "strikes", None) or set():
                    try:
                        sf = float(s)
                    except (TypeError, ValueError):
                        continue
                    if sf > 0:
                        strikes.add(sf)
            return sorted(strikes)
        except Exception:
            return []

    def fills(self, days: int = 7) -> list[dict]:
        """Return executed fills over the last `days` from IB.

        Each row: time, symbol, side, shares, price, commission, realized_pnl (if known).
        Source of truth for "what actually happened to the account".
        """
        import datetime as dt

        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        out = []
        for f in self.ib.fills():
            row = None
            try:
                t = f.execution.time
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.UTC)
                if t < cutoff:
                    continue
                comm = getattr(f.commissionReport, "commission", 0.0) or 0.0
                pnl = getattr(f.commissionReport, "realizedPNL", 0.0) or 0.0
                row = {
                    "time": str(t),
                    "symbol": f.contract.symbol,
                    "side": f.execution.side,  # BOT / SLD
                    "shares": int(f.execution.shares),
                    "price": float(f.execution.price),
                    "commission": float(comm),
                    "realized_pnl": float(pnl),
                    "exec_id": f.execution.execId,
                }
            except Exception:
                row = None
            if row is not None:
                out.append(row)
        return sorted(out, key=lambda r: r["time"])

    def account_summary(self) -> dict:
        """Snapshot of key account fields: equity, cash, realized P&L day, etc."""
        out = {}
        for row in self.ib.accountSummary():
            if row.tag in (
                "NetLiquidation",
                "TotalCashValue",
                "RealizedPnL",
                "UnrealizedPnL",
                "AvailableFunds",
                "GrossPositionValue",
                "BuyingPower",
            ):
                try:
                    out[row.tag] = float(row.value)
                except (ValueError, TypeError):
                    out[row.tag] = row.value
        return out

    def account_currency(self) -> str:
        """Base currency of the account (e.g. 'USD', 'AUD').

        Kept for backward compatibility with callers that just want the
        single base label. The currency-trade-guard predicate now uses
        account_cash_currencies() instead — see T-BOT-LIVE-CCY-GUARD-FIX1.
        Returns '' on lookup failure.
        """
        try:
            for row in self.ib.accountSummary():
                if row.tag == "NetLiquidation":
                    cur = (getattr(row, "currency", "") or "").strip().upper()
                    if cur and cur != "BASE":
                        return cur
            for row in self.ib.accountSummary():
                if row.tag == "AccountCurrency":
                    return str(row.value or "").upper().strip()
        except Exception:
            return ""
        return ""

    def account_cash_currencies(self) -> set[str]:
        """Currencies the account actually holds cash in (T-BOT-LIVE-CCY-
        GUARD-FIX1).

        Reads ib.accountValues() and collects the currency code of every
        cash-like row whose value is a positive number. Multi-currency
        IBKR accounts (e.g. AUD base + USD cash) need this set, not
        just the NetLiquidation base label, to decide whether a USD
        contract can be funded.

        Returns an empty set on any lookup failure. Callers treat empty
        as 'unknown' and only allow contracts via the explicit
        ACCEPTED_CONTRACT_CURRENCIES override.

        Cash-like tags inspected (defensive — IB has a few naming
        variants across account types):
          CashBalance / TotalCashBalance / TotalCashValue
          AvailableFunds / SettledCash
        """
        cash_tags = {
            "CashBalance",
            "TotalCashBalance",
            "TotalCashValue",
            "AvailableFunds",
            "SettledCash",
        }
        out: set[str] = set()
        try:
            for row in self.ib.accountValues():
                tag = getattr(row, "tag", "") or ""
                if tag not in cash_tags:
                    continue
                cur = (getattr(row, "currency", "") or "").strip().upper()
                if not cur or cur == "BASE":
                    continue
                raw = getattr(row, "value", "") or ""
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    out.add(cur)
        except Exception:
            return set()
        return out

    def rebalance(self, symbol: str, target: int) -> dict:
        """Adjust STOCK position to `target` shares. Long-only: refuses any
        path that would open or extend a short. Filters by secType='STK' so
        an option holding can't masquerade as a stock (root cause of the
        2026-05-08 naked-short incident)."""
        if target < 0:
            return {
                "status": "refused",
                "symbol": symbol,
                "reason": f"negative_target_would_short: target={target}",
            }
        current = self.position(symbol, sec_type="STK")
        delta = target - current
        if delta == 0:
            return {"status": "no_change", "symbol": symbol, "delta": 0}
        if current + delta < 0:
            return {
                "status": "refused",
                "symbol": symbol,
                "reason": f"would_short: current={current} target={target}",
            }
        action = "BUY" if delta > 0 else "SELL"
        order = MarketOrder(action, abs(delta))
        trade = self.ib.placeOrder(self._stock(symbol), order)
        return {
            "status": "submitted",
            "symbol": symbol,
            "delta": delta,
            "order_id": trade.order.orderId,
        }

    def place_option_order(
        self,
        symbol: str,
        expiry: str,
        strike: float,
        right: str = "C",
        action: str = "BUY",
        contracts: int = 1,
        order_type: str = "MID",
        limit_price: float | None = None,
        wait_secs: float = 3.0,
    ) -> dict:
        """Place an option order and WAIT briefly for IB confirmation/rejection.

        IB lifecycle: PendingSubmit → PreSubmitted → Submitted (working) OR
        Cancelled / ApiCancelled (e.g., Error 200 on illiquid contracts).
        Without a wait, this returned 'submitted' immediately even when IB
        async-cancelled the order ~1s later — caller logged phantom success.
        Now we wait up to wait_secs and surface real status.

        order_type:
          MID  - LMT at midpoint of NBBO (default). Replaces MIDPRICE which
                 IB rejects on options/SMART with Error 387.
          LMT  - explicit limit_price required
          MKT  - immediate market fill (pays full spread)
        """
        import time

        contract = Option(symbol, expiry, strike, right, "SMART")
        self.ib.qualifyContracts(contract)

        # Strike-grid / contract validation guard (T-P0-STRIKEGRID1).
        # ib_insync's qualifyContracts mutates the passed contract in place.
        # When IB cannot find a matching security definition (Error 200 —
        # strike not on the listed grid for this expiry, expired contract,
        # delisted symbol, …) it leaves conId == 0. Without this early
        # return, placeOrder still sends and IB asynchronously cancels with
        # "No security definition has been found" — that cancel can land
        # AFTER our wait_secs window, causing the caller to see
        # status="submitted" and write phantom tracked state. We refuse the
        # order outright when the contract did not qualify.
        if not getattr(contract, "conId", 0):
            return {
                "symbol": symbol,
                "expiry": expiry,
                "strike": strike,
                "right": right,
                "action": action,
                "contracts": contracts,
                "order_type": order_type,
                "status": "invalid_contract",
                "ib_status": "NoSecurityDefinition",
                "error": (
                    f"no security definition for {symbol} ${strike}{right} "
                    f"{expiry} (strike likely not on IB's listed grid for "
                    "this expiry)"
                ),
            }

        # Naked-short guard for SELL. Mirrors bc99208's stock-side rebalance
        # guard. 2026-05-08 incident: dashboard double-click on Untracked IB
        # options Sell button took ADI/ALB calls 1 -> 0 -> -1, opening naked
        # SHORT calls (unlimited risk). Refuse if we don't have the contract.
        if action == "SELL":
            held = self._option_position_qty(symbol, expiry, strike, right)
            if held <= 0:
                raise ValueError(
                    f"refused: held_qty={held} of {symbol} ${strike}{right} "
                    f"{expiry} — SELL would open naked short"
                )
            if contracts > held:
                raise ValueError(
                    f"refused: requested SELL {contracts} but only hold "
                    f"{held} of {symbol} ${strike}{right} {expiry}"
                )

        sent_as = order_type
        order: LimitOrder | MarketOrder
        if order_type == "MID":
            # Snap to midpoint of NBBO. Quote for ~1s, compute mid, place LMT.
            # Falls back to MKT if no quote — paper IB has no L1 option
            # subscription (Error 10089) so bid/ask come back 0/0; pre-market
            # and illiquid contracts hit the same path.
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(1.0)
            bid = float(ticker.bid) if ticker.bid and ticker.bid > 0 else 0.0
            ask = float(ticker.ask) if ticker.ask and ticker.ask > 0 else 0.0
            self.ib.cancelMktData(contract)
            if bid > 0 and ask > 0 and ask >= bid:
                mid = round((bid + ask) / 2.0, 2)
                order = LimitOrder(action, contracts, mid)
                sent_as = f"MID@{mid}"
            else:
                order = MarketOrder(action, contracts)
                sent_as = "MID->MKT_no_nbbo"
        elif order_type == "LMT":
            if limit_price is None:
                raise ValueError("limit_price required for LMT order")
            order = LimitOrder(action, contracts, limit_price)
            sent_as = f"LMT@{limit_price}"
        elif order_type == "MKT":
            order = MarketOrder(action, contracts)
        else:
            raise ValueError(f"unknown order_type: {order_type}")

        trade = self.ib.placeOrder(contract, order)

        deadline = time.time() + wait_secs
        terminal = ("Filled", "Cancelled", "ApiCancelled", "Inactive")
        while time.time() < deadline:
            self.ib.sleep(0.3)
            st = trade.orderStatus.status
            if st in terminal or st == "Submitted":
                break

        st = trade.orderStatus.status or "Unknown"
        base = {
            "symbol": symbol,
            "expiry": expiry,
            "strike": strike,
            "right": right,
            "action": action,
            "contracts": contracts,
            "order_type": order_type,
            "sent_as": sent_as,
            "order_id": trade.order.orderId,
            "ib_status": st,
        }
        if st in ("Cancelled", "ApiCancelled", "Inactive"):
            err_msg = trade.log[-1].message if trade.log else "unknown"
            base["status"] = "cancelled"
            base["error"] = err_msg
            return base
        base["status"] = "submitted"
        return base
