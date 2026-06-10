"""In-memory broker for backtesting. Matches IBKRClient's surface.

v1.1 change: models **slippage** on fills. Breakouts have adverse selection
(buying into strength), so fills are systematically worse than the signal price.
Default 25 bps per side — buys fill higher, sells fill lower.
"""

from __future__ import annotations


class MockBroker:
    def __init__(
        self,
        starting_equity: float = 100_000.0,
        commission_bps: float = 0.0,
        slippage_bps: float = 25.0,
        margin_multiple: float = 1.0,
    ):
        # Margin: if 1.3, bot can use up to 1.3x equity in notional positions.
        # Implemented by allowing `cash` to go negative down to equity × (1 - margin_multiple).
        # Interest on borrowed portion deducted at MARGIN_INTEREST_RATE annually.
        self.cash = starting_equity
        self._starting_equity = starting_equity
        self._positions: dict[str, int] = {}
        self._prices: dict[str, float] = {}
        self._commission = commission_bps / 10_000.0
        self._slippage = slippage_bps / 10_000.0
        self._margin_multiple = margin_multiple
        self.trade_log: list[dict] = []

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    def equity(self) -> float:
        mkt = sum(shares * self._prices.get(sym, 0.0) for sym, shares in self._positions.items())
        return float(self.cash + mkt)

    def buying_power(self) -> float:
        """How much can the bot spend before hitting the margin limit."""
        return self.equity() * self._margin_multiple - sum(
            shares * self._prices.get(sym, 0.0) for sym, shares in self._positions.items()
        )

    def apply_daily_margin_interest(self, annual_rate: float = 0.06) -> None:
        """Charge interest on any negative cash (borrowed margin)."""
        if self.cash < 0:
            daily = (1 + annual_rate) ** (1 / 252) - 1
            self.cash *= 1 + daily

    def position(self, symbol: str) -> int:
        return int(self._positions.get(symbol, 0))

    def market_price(self, symbol: str) -> float:
        return float(self._prices.get(symbol, 0.0))

    def rebalance(self, symbol: str, target: int) -> dict:
        current = self._positions.get(symbol, 0)
        delta = target - current
        if delta == 0:
            return {"status": "no_change", "symbol": symbol, "delta": 0}
        signal_price = self._prices.get(symbol, 0.0)
        if signal_price <= 0:
            return {"status": "no_price", "symbol": symbol, "delta": delta}

        # Slippage: buys fill higher, sells fill lower
        fill_price = signal_price * (1 + self._slippage if delta > 0 else 1 - self._slippage)
        notional = abs(delta) * fill_price
        fee = notional * self._commission
        # Margin check: can we afford this buy given margin_multiple?
        if delta > 0 and self._margin_multiple > 1.0:
            if notional > self.buying_power():
                return {
                    "status": "insufficient_margin",
                    "symbol": symbol,
                    "delta": delta,
                    "required": notional,
                    "available": self.buying_power(),
                }
        elif delta > 0 and notional > self.cash:
            return {
                "status": "insufficient_cash",
                "symbol": symbol,
                "delta": delta,
                "required": notional,
                "available": self.cash,
            }
        self.cash -= delta * fill_price + fee
        self._positions[symbol] = target
        if target == 0:
            self._positions.pop(symbol, None)

        rec = {
            "status": "submitted",
            "symbol": symbol,
            "delta": delta,
            "signal_price": signal_price,
            "fill_price": fill_price,
            "slippage_cost": abs(delta) * (fill_price - signal_price)
            if delta > 0
            else abs(delta) * (signal_price - fill_price),
            "fee": fee,
            "new_position": target,
        }
        self.trade_log.append(rec)
        return rec

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def set_prices(self, prices: dict[str, float]) -> None:
        self._prices.update(prices)
