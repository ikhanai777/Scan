"""Telegram bot alerts -- the fastest channel to ship, per the spec.

Setup: talk to @BotFather to create a bot, take the token, message the bot
once, then read your chat id from
https://api.telegram.org/bot<token>/getUpdates. Put both in CS_TELEGRAM_BOT_TOKEN
and CS_TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings
from ..models import Signal
from ..tracker import TrackerUpdate
from . import format_resolution, format_signal

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0


class TelegramNotifier:
    name = "telegram"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def signal_fired(self, signal: Signal) -> None:
        self._send(format_signal(signal, self.settings))

    def signal_resolved(self, update: TrackerUpdate) -> None:
        self._send(format_resolution(update))

    def _send(self, text: str) -> None:
        if self.settings.dry_run:
            log.info("[dry-run] telegram message:\n%s", text)
            return
        url = f"{API}/bot{self.settings.telegram_bot_token}/sendMessage"
        response = self._client.post(
            url,
            json={
                "chat_id": self.settings.telegram_chat_id,
                # <pre> keeps the level columns aligned in the app, and means
                # a coin ticker with an underscore cannot break the parse.
                "text": f"<pre>{_escape(text)}</pre>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if response.status_code >= 400:
            # Raise so NotifierGroup logs it with the channel name; the scan
            # cycle carries on either way.
            raise RuntimeError(f"telegram returned {response.status_code}: {response.text[:200]}")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
