"""Generic JSON webhook: the same card, as structured data.

Anything that can receive a POST -- a Discord relay, a Zapier hook, your own
service -- takes the signal card verbatim from `to_public_dict`, so a consumer
never has to parse the text format.
"""

from __future__ import annotations

import httpx

from ..config import Settings
from ..models import Signal
from ..tracker import TrackerUpdate

TIMEOUT_SECONDS = 10.0


class WebhookNotifier:
    name = "webhook"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def signal_fired(self, signal: Signal) -> None:
        self._post({
            "event": "signal.fired",
            "signal": signal.to_public_dict(),
            "disclaimer": self.settings.DISCLAIMER,
        })

    def signal_resolved(self, update: TrackerUpdate) -> None:
        self._post({
            "event": f"signal.{update.kind}",
            "price": update.price,
            "detail": update.detail,
            "signal": update.signal.to_public_dict(),
            "disclaimer": self.settings.DISCLAIMER,
        })

    def _post(self, payload: dict) -> None:
        if self.settings.dry_run:
            return
        response = self._client.post(self.settings.webhook_url, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"webhook returned {response.status_code}")
