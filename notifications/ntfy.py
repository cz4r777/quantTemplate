"""ntfy.sh push notifications.

Posts a message to a ntfy topic. Operator subscribes via the ntfy app
(iOS / Android) — no server install needed.

Setup:
  1. Install 'ntfy' app on your phone
  2. Pick a long random topic name (it's your password — anyone with the
     name can send you notifications). e.g. tradingbot-z9x4k7m2-prod
  3. Subscribe to that topic in the app
  4. Set NTFY_TOPIC env var (or in .env): NTFY_TOPIC=your-topic-here
  5. Done — bot now posts trade events / EOD reports / alerts to your phone

Self-hosted alternative: if you'd rather not use ntfy.sh, set
NTFY_BASE_URL to your own ntfy server (e.g. https://ntfy.your-domain.com).

Free tier of ntfy.sh allows ~250 messages/day per topic — fine for our use.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx


def send(
    message: str,
    title: str = "tradingbot",
    priority: str = "default",
    tags: str = "",
) -> bool:
    """Post a message to the configured ntfy topic.

    priority: min | low | default | high | urgent
    tags: comma-separated emoji tags (e.g. "warning,money") — see ntfy docs

    Returns True on success, False otherwise. Failure is silent — never
    breaks the bot.
    """
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    base = os.getenv("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    url = f"{base}/{topic}"

    headers = {
        "Title": title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = tags

    try:
        response = httpx.post(
            url,
            content=message.encode("utf-8"),
            headers=headers,
            timeout=5.0,
        )
        return response.is_success
    except httpx.HTTPError:
        return False
