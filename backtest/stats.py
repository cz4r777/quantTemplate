"""Backtest statistics — returns, drawdown, win rate, etc."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


@dataclass
class Stats:
    starting_equity: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe: float
    trading_days: int
    num_trades: int
    num_exits: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_win_loss_ratio: float
    time_in_market_pct: float
    yearly_returns: list[dict] = field(
        default_factory=list
    )  # [{year: "2024", return_pct: 41.1, start: $, end: $}]

    def to_dict(self) -> dict:
        return asdict(self)


def yearly_breakdown(curve: list[dict]) -> list[dict]:
    """Group equity curve by year and compute each year's return %."""
    by_year: dict[str, list[float]] = {}
    for row in curve:
        year = str(row["date"])[:4]
        by_year.setdefault(year, []).append(row["equity"])
    out = []
    for year in sorted(by_year.keys()):
        start = by_year[year][0]
        end = by_year[year][-1]
        out.append(
            {
                "year": year,
                "start": round(start, 2),
                "end": round(end, 2),
                "return_pct": round((end - start) / start * 100, 2) if start > 0 else 0,
            }
        )
    return out


def _daily_returns(curve: list[dict]) -> list[float]:
    returns = []
    for i in range(1, len(curve)):
        prev = curve[i - 1]["equity"]
        cur = curve[i]["equity"]
        if prev > 0:
            returns.append(cur / prev - 1.0)
    return returns


def _max_drawdown(curve: list[dict]) -> float:
    peak = 0.0
    worst = 0.0
    for row in curve:
        eq = row["equity"]
        peak = max(peak, eq)
        if peak > 0:
            worst = min(worst, eq / peak - 1.0)
    return -worst


def _sharpe(daily: list[float], rf: float = 0.0) -> float:
    if not daily:
        return 0.0
    mean = sum(daily) / len(daily)
    var = sum((r - mean) ** 2 for r in daily) / len(daily)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean - rf) / std * math.sqrt(252)


def _round_trip_pnl(events: list[dict]) -> list[float]:
    """Group entries and exits by symbol into round-trip % returns."""
    by_sym: dict[str, list] = {}
    for e in events:
        by_sym.setdefault(e["symbol"], []).append(e)

    pnls = []
    for _sym, evts in by_sym.items():
        entry_price = None
        for e in evts:
            action = e.get("action")
            if action == "pilot":
                entry_price = e.get("entry")
            elif action == "exit" and entry_price is not None:
                exit_price = e.get("price")
                if exit_price and entry_price > 0:
                    pnls.append((exit_price - entry_price) / entry_price)
                entry_price = None
    return pnls


def compute(result) -> Stats:
    curve = result.equity_curve
    starting = result.starting_equity
    final = result.final_equity

    trading_days = len(curve)
    daily = _daily_returns(curve)
    total_ret = (final / starting - 1.0) if starting else 0.0
    years = max(trading_days / 252.0, 1 / 252.0)
    cagr = (final / starting) ** (1 / years) - 1.0 if starting > 0 else 0.0
    mdd = _max_drawdown(curve)
    sharpe = _sharpe(daily)

    pnls = _round_trip_pnl(result.events)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) if pnls else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    ratio = abs(avg_win / avg_loss) if avg_loss < 0 else 0.0

    time_in_market = (
        sum(1 for row in curve if row.get("open_positions", 0) > 0) / trading_days
        if trading_days
        else 0.0
    )

    return Stats(
        starting_equity=starting,
        final_equity=final,
        total_return_pct=total_ret * 100,
        cagr_pct=cagr * 100,
        max_drawdown_pct=mdd * 100,
        sharpe=sharpe,
        trading_days=trading_days,
        num_trades=len(result.trades),
        num_exits=len(pnls),
        win_rate=win_rate * 100,
        avg_win_pct=avg_win * 100,
        avg_loss_pct=avg_loss * 100,
        avg_win_loss_ratio=ratio,
        time_in_market_pct=time_in_market * 100,
        yearly_returns=yearly_breakdown(curve),
    )
