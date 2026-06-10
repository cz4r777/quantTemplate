"""Pure notification message schema + formatters.

This module is a deterministic formatter layer for trade events and
diagnostic alerts. It produces a stable, grep-friendly payload shape so
downstream consumers (the smsbot sender, the dashboard, the daily report,
future routing layers) can rely on a single source of message text without
each one re-inventing wording.

Design rules (enforced by tests):

  * Pure: no I/O, no broker calls, no environment reads, no secrets, no
    delivery side-effects. Every builder returns a Message.
  * Stable shape: every Message has event_type, title, body, severity, tags.
  * Deterministic: same inputs -> same payload bytes.
  * Defensive: optional fields that are absent degrade to "?" placeholders
    rather than raising.

This module does NOT change how notifications are sent. The existing
notifications.smsbot.send() function is unchanged and not imported here.
"""

from __future__ import annotations

import dataclasses
from typing import Any

# Severity vocabulary — kept to 3 levels on purpose. Downstream layers
# can map these onto channels (e.g. smsbot categories) without this module
# needing to know about them.
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
ALLOWED_SEVERITIES = frozenset({SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO})

# Routing-hint tags. Downstream sender chooses a channel based on these.
# Matches the smsbot category vocabulary (alerts | trades | warnings |
# status) so a future sending-layer ticket can wire it through without
# re-naming anything.
TAG_ALERTS = "alerts"
TAG_TRADES = "trades"
TAG_WARNINGS = "warnings"
TAG_STATUS = "status"
TAG_TRADE = "trade"
TAG_OPTION = "option"
TAG_STOCK = "stock"
TAG_GATEWAY = "gateway"
TAG_DATA_FEED = "data_feed"
TAG_WATCHLIST = "watchlist"


@dataclasses.dataclass(frozen=True)
class Message:
    event_type: str
    title: str
    body: str
    severity: str
    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"unknown severity {self.severity!r}; expected one of {sorted(ALLOWED_SEVERITIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["tags"] = list(self.tags)
        return d


# --- helpers -----------------------------------------------------------------


def _opt(value: Any, fallback: str = "?") -> str:
    """Render an optional field for human-facing text without raising on None."""
    if value is None:
        return fallback
    if isinstance(value, float):
        # Money / price fields render with 2 decimals; let callers pre-format
        # if they want different precision.
        return f"{value:.2f}"
    return str(value)


def _instrument_tag(right: Any, strike: Any, expiry: Any) -> str:
    """An option is anything with option-like coordinates."""
    if right or strike or expiry:
        return TAG_OPTION
    return TAG_STOCK


def _option_label(symbol: str, expiry: Any, strike: Any, right: Any) -> str:
    """Render `SYM YYYYMMDD $STRIKE C/P` if option fields present, else SYM."""
    if not (expiry or strike or right):
        return symbol
    parts = [symbol]
    if expiry:
        parts.append(str(expiry))
    if strike is not None:
        parts.append(f"${_opt(strike)}")
    if right:
        parts.append(str(right))
    return " ".join(parts)


# --- trade-lifecycle builders -----------------------------------------------


def trade_submitted(
    symbol: str,
    action: str,
    qty: Any,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
    order_type: Any = None,
    limit_price: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    body_bits = [f"submitted {action} {_opt(qty)} {label}"]
    if order_type:
        body_bits.append(f"type={order_type}")
    if limit_price is not None:
        body_bits.append(f"@${_opt(limit_price)}")
    return Message(
        event_type="TRADE_SUBMITTED",
        title=f"[{symbol}] {action} submitted",
        body=" ".join(body_bits),
        severity=SEVERITY_INFO,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_TRADES),
    )


def trade_filled(
    symbol: str,
    action: str,
    qty: Any,
    price: Any,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    return Message(
        event_type="TRADE_FILLED",
        title=f"[{symbol}] {action} filled",
        body=f"filled {action} {_opt(qty)} {label} @ ${_opt(price)}",
        severity=SEVERITY_INFO,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_TRADES),
    )


def trade_partial(
    symbol: str,
    action: str,
    qty_filled: Any,
    qty_total: Any,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
    price: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    body_bits = [f"partial {action} {_opt(qty_filled)}/{_opt(qty_total)} {label}"]
    if price is not None:
        body_bits.append(f"@ ${_opt(price)}")
    return Message(
        event_type="TRADE_PARTIAL",
        title=f"[{symbol}] {action} partial fill",
        body=" ".join(body_bits),
        severity=SEVERITY_WARNING,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_WARNINGS),
    )


def trade_cancelled(
    symbol: str,
    action: str,
    reason: Any = None,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
    qty: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    body = f"cancelled {action} {_opt(qty)} {label}"
    if reason:
        body += f" — reason: {reason}"
    return Message(
        event_type="TRADE_CANCELLED",
        title=f"[{symbol}] {action} cancelled",
        body=body,
        severity=SEVERITY_CRITICAL,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_ALERTS),
    )


def trade_refused(
    symbol: str,
    action: str,
    reason: Any,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
    qty: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    return Message(
        event_type="TRADE_REFUSED",
        title=f"[{symbol}] {action} refused",
        body=f"refused {action} {_opt(qty)} {label} — reason: {_opt(reason)}",
        severity=SEVERITY_CRITICAL,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_ALERTS),
    )


def position_opened(
    symbol: str,
    qty: Any,
    entry: Any,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
    stop: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    body_bits = [f"opened {label} qty={_opt(qty)} entry=${_opt(entry)}"]
    if stop is not None:
        body_bits.append(f"stop=${_opt(stop)}")
    return Message(
        event_type="POSITION_OPENED",
        title=f"[{symbol}] position opened",
        body=" ".join(body_bits),
        severity=SEVERITY_INFO,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_TRADES),
    )


def position_closed(
    symbol: str,
    qty: Any,
    exit_price: Any,
    *,
    expiry: Any = None,
    strike: Any = None,
    right: Any = None,
    pnl_pct: Any = None,
    reason: Any = None,
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    body_bits = [f"closed {label} qty={_opt(qty)} exit=${_opt(exit_price)}"]
    if pnl_pct is not None:
        body_bits.append(f"pnl={_opt(pnl_pct)}%")
    if reason:
        body_bits.append(f"reason={reason}")
    return Message(
        event_type="POSITION_CLOSED",
        title=f"[{symbol}] position closed",
        body=" ".join(body_bits),
        severity=SEVERITY_INFO,
        tags=(TAG_TRADE, _instrument_tag(right, strike, expiry), TAG_TRADES),
    )


# --- diagnostic / operator alerts -------------------------------------------


def exercise_risk(
    symbol: str, expiry: Any, dte: Any, *, strike: Any = None, right: Any = None, qty: Any = None
) -> Message:
    label = _option_label(symbol, expiry, strike, right)
    return Message(
        event_type="EXERCISE_RISK",
        title=f"[{symbol}] option exercise / assignment risk",
        body=f"{label} qty={_opt(qty)} DTE={_opt(dte)} — review before expiry",
        severity=SEVERITY_CRITICAL,
        tags=(TAG_OPTION, TAG_ALERTS),
    )


def gateway_down(
    host: Any = None,
    port: Any = None,
    *,
    detail: Any = None,
    streak: Any = None,
    display: Any = None,
) -> Message:
    where = f"{_opt(host, 'gateway')}:{_opt(port, '?')}"
    body = f"IB Gateway unreachable at {where}"
    extras: list[str] = []
    if streak is not None:
        extras.append(f"streak={_opt(streak)}")
    if display:
        extras.append(f"display={display}")
    if detail:
        extras.append(str(detail))
    if extras:
        body += " — " + " | ".join(extras)
    return Message(
        event_type="GATEWAY_DOWN",
        title="IB Gateway down",
        body=body,
        severity=SEVERITY_CRITICAL,
        tags=(TAG_GATEWAY, TAG_ALERTS),
    )


def gateway_recovered(
    host: Any = None,
    port: Any = None,
    *,
    recovery_secs: Any = None,
    attempts: Any = None,
    display: Any = None,
    detail: Any = None,
) -> Message:
    """Watchdog observed the gateway port come back up after a down period."""
    where = f"{_opt(host, 'gateway')}:{_opt(port, '?')}"
    body = f"IB Gateway recovered at {where}"
    extras: list[str] = []
    if recovery_secs is not None:
        extras.append(f"took={_opt(recovery_secs)}s")
    if attempts is not None:
        extras.append(f"attempts={_opt(attempts)}")
    if display:
        extras.append(f"display={display}")
    if detail:
        extras.append(str(detail))
    if extras:
        body += " — " + " | ".join(extras)
    return Message(
        event_type="GATEWAY_RECOVERED",
        title="IB Gateway recovered",
        body=body,
        severity=SEVERITY_INFO,
        tags=(TAG_GATEWAY, TAG_STATUS),
    )


def data_feed_unhealthy(
    provider: Any, ratio: Any = None, *, missing: Any = None, detail: Any = None
) -> Message:
    body_bits = [f"data feed {_opt(provider)} unhealthy"]
    if ratio is not None:
        body_bits.append(f"healthy_ratio={_opt(ratio)}")
    if missing is not None:
        body_bits.append(f"missing={_opt(missing)}")
    if detail:
        body_bits.append(str(detail))
    return Message(
        event_type="DATA_FEED_UNHEALTHY",
        title=f"data feed {_opt(provider)} unhealthy",
        body=" — ".join(body_bits),
        severity=SEVERITY_CRITICAL,
        tags=(TAG_DATA_FEED, TAG_ALERTS),
    )


def watchlist_stale(
    source: Any,
    as_of: Any = None,
    *,
    count: Any = None,
    detail: Any = None,
    fallback_used: Any = None,
) -> Message:
    body_bits = [f"watchlist source {_opt(source)} stale"]
    if as_of is not None:
        body_bits.append(f"as_of={_opt(as_of)}")
    if count is not None:
        body_bits.append(f"count={_opt(count)}")
    if fallback_used is not None:
        body_bits.append(f"fallback_used={_opt(fallback_used)}")
    if detail:
        body_bits.append(str(detail))
    return Message(
        event_type="WATCHLIST_STALE",
        title=f"watchlist {_opt(source)} stale",
        body=" — ".join(body_bits),
        severity=SEVERITY_WARNING,
        tags=(TAG_WATCHLIST, TAG_WARNINGS),
    )


# Public registry — useful for tests and for future dispatch tables.
BUILDERS: dict[str, Any] = {
    "TRADE_SUBMITTED": trade_submitted,
    "TRADE_FILLED": trade_filled,
    "TRADE_PARTIAL": trade_partial,
    "TRADE_CANCELLED": trade_cancelled,
    "TRADE_REFUSED": trade_refused,
    "POSITION_OPENED": position_opened,
    "POSITION_CLOSED": position_closed,
    "EXERCISE_RISK": exercise_risk,
    "GATEWAY_DOWN": gateway_down,
    "GATEWAY_RECOVERED": gateway_recovered,
    "DATA_FEED_UNHEALTHY": data_feed_unhealthy,
    "WATCHLIST_STALE": watchlist_stale,
}

__all__ = [
    "Message",
    "ALLOWED_SEVERITIES",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
    "TAG_ALERTS",
    "TAG_TRADES",
    "TAG_WARNINGS",
    "TAG_STATUS",
    "TAG_TRADE",
    "TAG_OPTION",
    "TAG_STOCK",
    "TAG_GATEWAY",
    "TAG_DATA_FEED",
    "TAG_WATCHLIST",
    "BUILDERS",
    "trade_submitted",
    "trade_filled",
    "trade_partial",
    "trade_cancelled",
    "trade_refused",
    "position_opened",
    "position_closed",
    "exercise_risk",
    "gateway_down",
    "gateway_recovered",
    "data_feed_unhealthy",
    "watchlist_stale",
]
