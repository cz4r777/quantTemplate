"""Execution-event logger — captures every broker round-trip in detail.

Designed to surface bugs in the broker integration that would otherwise
take weeks to find on v1.2's slow trade cadence. Every intraday entry,
exit, trim, position state transition gets a structured log row.

Output: JSONL at state/exec_log.jsonl (one row per event).

Each row has:
  ts             — wall-clock timestamp of the log call
  bar_time       — the simulated bar time (or live bar)
  symbol
  action         — "open" | "trim" | "exit" | "exit_eod" | "broker_call"
                   | "broker_reject" | "state_mutation"
  payload        — full broker rebalance() return dict
  position_before, position_after  — position dict snapshots
  expected_pnl   — what we think P&L should be after this action
  notes          — free-form context

Tools to consume the log:
  scripts/exec_audit.py   — replay log entries, find inconsistencies
                            (phantom positions, P&L mismatches, etc.)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_LOG_PATH = Path("state/exec_log.jsonl")


def _ensure_dir() -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def reset() -> None:
    """Clear the log file (call at the start of a backtest run)."""
    _ensure_dir()
    if _LOG_PATH.exists():
        _LOG_PATH.unlink()


def log(
    action: str,
    symbol: str,
    bar_time: str = "",
    payload: dict | None = None,
    position_before: dict | None = None,
    position_after: dict | None = None,
    expected_pnl: float | None = None,
    notes: str = "",
) -> None:
    """Append one structured row to the exec log."""
    _ensure_dir()
    row = {
        "ts": datetime.utcnow().isoformat(),
        "bar_time": bar_time,
        "symbol": symbol,
        "action": action,
        "payload": payload or {},
        "position_before": position_before,
        "position_after": position_after,
        "expected_pnl": expected_pnl,
        "notes": notes,
    }
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def tail(n: int = 50) -> list[dict]:
    """Read the last N rows."""
    if not _LOG_PATH.exists():
        return []
    lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:] if line.strip()]


def all_rows() -> list[dict]:
    """Read all rows."""
    if not _LOG_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in _LOG_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def wrap_broker(broker) -> None:
    """Monkey-patch broker.rebalance() to auto-log every call.

    Use at startup:
        from execution import exec_log
        exec_log.wrap_broker(broker)

    After this, every broker.rebalance() call will append a row to
    state/exec_log.jsonl with the full result payload + position context.
    Idempotent — won't double-wrap.
    """
    if getattr(broker, "_exec_log_wrapped", False):
        return

    # ---- rebalance (stock orders) ----
    original_rebalance = broker.rebalance

    def wrapped_rebalance(symbol, target, *args, **kwargs):
        rec = original_rebalance(symbol, target, *args, **kwargs)
        try:
            current_pos = broker.position(symbol) if hasattr(broker, "position") else None
        except Exception:
            current_pos = None
        log(
            action="broker_rebalance",
            symbol=symbol,
            payload=rec,
            position_after={"shares": current_pos} if current_pos is not None else None,
            notes=f"target={target}",
        )
        return rec

    broker.rebalance = wrapped_rebalance

    # ---- place_option_order (options) ----
    # Without this, option entries/exits leave NO audit trail. We saw a
    # 2026-05-02 incident where positions.json had option entries but
    # exec_log.jsonl had zero matching action records.
    if hasattr(broker, "place_option_order"):
        original_opt = broker.place_option_order

        def wrapped_option_order(*args, **kwargs):
            try:
                rec = original_opt(*args, **kwargs)
            except Exception as e:
                log(
                    action="broker_place_option_order_FAILED",
                    symbol=kwargs.get("symbol") or (args[0] if args else "?"),
                    payload={
                        "error": str(e),
                        "kwargs": {k: kwargs[k] for k in kwargs if k != "limit_price"},
                    },
                    notes="option order raised exception",
                )
                raise
            log(
                action=f"broker_option_{kwargs.get('action', 'BUY').lower()}",
                symbol=kwargs.get("symbol") or rec.get("symbol", "?"),
                payload=rec,
                notes=(
                    f"option {kwargs.get('action', 'BUY')} "
                    f"{kwargs.get('contracts', '?')}x ${kwargs.get('strike', '?')}"
                    f"{kwargs.get('right', 'C')} {kwargs.get('expiry', '?')}"
                ),
            )
            return rec

        broker.place_option_order = wrapped_option_order

    broker._exec_log_wrapped = True
