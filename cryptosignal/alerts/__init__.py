"""Delivery: push the card the instant it fires, and again when it resolves.

A channel that is not configured is simply absent from the notifier list, and a
channel that throws is logged and skipped -- a Telegram outage must never stop
a scan cycle or lose a signal. The database is the record; alerts are a copy.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import Settings
from ..models import Signal
from ..tracker import TrackerUpdate

log = logging.getLogger(__name__)


class Notifier(Protocol):
    name: str

    def signal_fired(self, signal: Signal) -> None: ...
    def signal_resolved(self, update: TrackerUpdate) -> None: ...


class NotifierGroup:
    """Fans out to every configured channel, surviving any one of them failing."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def __len__(self) -> int:
        return len(self.notifiers)

    @property
    def names(self) -> list[str]:
        return [n.name for n in self.notifiers]

    def signal_fired(self, signal: Signal) -> None:
        for notifier in self.notifiers:
            try:
                notifier.signal_fired(signal)
            except Exception:
                log.exception("notifier %s failed on signal_fired", notifier.name)

    def signal_resolved(self, update: TrackerUpdate) -> None:
        for notifier in self.notifiers:
            try:
                notifier.signal_resolved(update)
            except Exception:
                log.exception("notifier %s failed on signal_resolved", notifier.name)


def build_notifiers(settings: Settings) -> NotifierGroup:
    from .telegram import TelegramNotifier
    from .webhook import WebhookNotifier

    notifiers: list[Notifier] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifiers.append(TelegramNotifier(settings))
    else:
        log.info("telegram alerts disabled: CS_TELEGRAM_BOT_TOKEN / CS_TELEGRAM_CHAT_ID not set")
    if settings.webhook_url:
        notifiers.append(WebhookNotifier(settings))
    return NotifierGroup(notifiers)


def format_signal(signal: Signal, settings: Settings) -> str:
    """The signal card as plain text, for any channel that takes text."""
    arrow = "LONG" if signal.direction.value == "long" else "SHORT"
    lines = [
        f"{arrow}  {signal.symbol}   {signal.confidence:.0f}% confidence",
        "",
        f"entry   {signal.levels.entry_low:.6g} - {signal.levels.entry_high:.6g}",
        f"stop    {signal.levels.stop:.6g}",
        f"target  {signal.levels.target1:.6g}  ({signal.levels.reward_risk(signal.levels.target1):.1f}R)",
        f"        {signal.levels.target2:.6g}  ({signal.levels.reward_risk(signal.levels.target2):.1f}R)",
        f"window  {_humanise(signal.levels.hold_minutes)}",
        "",
        "why:",
    ]
    for driver in signal.drivers:
        lines.append(f"  - {driver.label} {driver.score:+.0f}: {driver.detail}")
    if signal.reduced_confidence:
        lines += ["", f"reduced confidence: {signal.reduced_confidence_reason}"]
    lines += ["", settings.DISCLAIMER]
    return "\n".join(lines)


def format_resolution(update: TrackerUpdate) -> str:
    signal = update.signal
    realized = f"{signal.realized_r:+.2f}R" if signal.realized_r is not None else "running"
    headline = {
        "target1": "TARGET 1 HIT",
        "target": "TARGET HIT",
        "stop": "STOPPED OUT",
        "expired": "WINDOW EXPIRED",
    }.get(update.kind, update.kind.upper())
    return (
        f"{headline}  {signal.symbol} {signal.direction.value}\n"
        f"{update.detail}\n"
        f"price {update.price:.6g}   result {realized}"
    )


def _humanise(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h" if remainder == 0 else f"{hours}h{remainder:02d}m"
