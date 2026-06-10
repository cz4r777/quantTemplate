"""Append-only audit log — every decision the bot makes.

Each cycle writes one JSON-per-line event. Used for debugging paper-trading
behavior: 'why did the bot skip AMD today?' has an answer.

    log(event_type, symbol=None, **data)

File: state/decisions.jsonl  (gitignored via state/)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

LOG_FILE = Path("state/decisions.jsonl")


def log(event_type: str, symbol: str | None = None, **data) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_type,
        "symbol": symbol,
        **data,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def tail(n: int = 50) -> list[dict]:
    """Return the last n events (for dashboard display)."""
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
