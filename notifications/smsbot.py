"""smsbot — kept as a name for backwards compatibility.

Now delegates to ntfy.sh (see notifications/ntfy.py). All existing
`from notifications.smsbot import send` imports keep working unchanged.

We dropped the local-uvicorn smsbot server path because:
  * ntfy.sh covers the same use case (push to phone)
  * Free, no Twilio cost
  * No local server to maintain on Kali (smsbot ran on Windows; tradingbot
    on Kali was POSTing to a 127.0.0.1:8001 that didn't exist there)

If you ever want a Twilio/SMS path back, restore the previous version
from git (commit history) and run uvicorn smsbot:server:app on the
relevant machine.
"""

from __future__ import annotations

from notifications.ntfy import send as _ntfy_send


def send(message: str, category: str = "status") -> None:
    """Backwards-compat shim. category becomes the ntfy priority/tags."""
    priority_map = {
        "alerts": "high",
        "warnings": "high",
        "trades": "default",
        "status": "default",
    }
    tags_map = {
        "alerts": "rotating_light",
        "warnings": "warning",
        "trades": "money_with_wings",
        "status": "robot",
    }
    _ntfy_send(
        message,
        title=f"tradingbot ({category})",
        priority=priority_map.get(category, "default"),
        tags=tags_map.get(category, ""),
    )


# T-NOTIFY-WIRE1 — formatter-driven dispatch.
#
# send_message() accepts a notifications.trade_messages.Message (the pure
# schema layer) and routes it through send() above. The routing tag is
# derived from msg.tags so the formatter never embeds environment- or
# transport-specific values. ntfy failure stays silent — notifications
# are reporting-only and must NEVER alter trading behavior.
def send_message(msg) -> None:  # msg: notifications.trade_messages.Message
    """Dispatch a formatter Message via the existing send() path.

    Looks for a routing-channel tag in msg.tags (alerts | warnings |
    trades | status); falls back to a severity-based default so a
    Message without an explicit channel tag still gets a sensible
    priority. Any exception is swallowed: notification failure must
    never break a trading or refusal path.
    """
    try:
        tags = tuple(getattr(msg, "tags", ()) or ())
        for tag in ("alerts", "warnings", "trades", "status"):
            if tag in tags:
                category = tag
                break
        else:
            sev = getattr(msg, "severity", "info")
            category = (
                "alerts"
                if sev == "critical"
                else "warnings"
                if sev == "warning"
                else "trades"
                if "trade" in tags
                else "status"
            )
        title = str(getattr(msg, "title", "") or "")
        body = str(getattr(msg, "body", "") or "")
        text = f"{title} — {body}" if title and body else (title or body)
        if not text:
            return
        send(text, category)
    except Exception:
        # Notification failures must never break trading logic.
        return None


# T-NOTIFY-WIRE2 — bash-callable bridge for the gateway watchdog.
#
# Usage from bash:
#   python3 -m notifications.smsbot _emit gateway_down \
#       --port=4002 --streak=3 --detail="sustained outage"
#
# Returns 0 always. Failure is silent — the watchdog must never wait on
# Python's notification dispatch to make recovery decisions.
def _emit_from_cli(argv=None) -> int:
    import sys as _sys

    if argv is None:
        argv = _sys.argv[1:]
    if len(argv) < 2 or argv[0] != "_emit":
        return 0
    event = argv[1]
    kwargs: dict = {}
    for token in argv[2:]:
        if not token.startswith("--"):
            continue
        key, sep, value = token[2:].partition("=")
        if not sep:
            continue
        coerced: object = value
        try:
            coerced = int(value)
        except ValueError:
            try:
                coerced = float(value)
            except ValueError:
                pass
        kwargs[key] = coerced
    try:
        from notifications.trade_messages import BUILDERS

        builder = BUILDERS.get(event.upper()) or BUILDERS.get(event)
        if builder is None:
            return 1  # unknown event — caller may fall back to legacy path
        msg = builder(**kwargs)
        send_message(msg)
        return 0
    except Exception:
        # Any unhandled error — signal to the bash caller so its
        # legacy curl path can deliver the notification anyway.
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via bash watchdog
    raise SystemExit(_emit_from_cli())
