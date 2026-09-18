"""External data sources, one module per provider.

Every source here follows the same contract, and it is the contract that makes
the whole three-leg design work: **a source that has no data returns None, and
None means abstain.** It never returns a neutral zero, never a placeholder,
never a guess. The leg above renormalises over whatever actually reported.

That is also why this app can run with no API keys at all. Funding rate and
open interest come from the same exchange the price does; TVL, the Fear &
Greed index and the news feeds are public and keyless. Sources that do need a
key stay silent until they have one, and their absence costs accuracy rather
than correctness.

Nothing in this package fabricates a value to fill a gap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 12.0
USER_AGENT = "cryptosignal/0.1 (+https://github.com/ikhanai777/Scan)"


@dataclass
class _Entry:
    value: Any
    expires_at: float


class HTTPSource:
    """A polite JSON client: one shared connection pool, TTL cache, no raising.

    Market data has to keep flowing when a secondary source is down, so every
    fetch failure here is logged and reported as None rather than raised. The
    scan cycle must never die because a news feed had a bad minute.
    """

    #: Subclasses set this so logs and the dashboard can name the source.
    name = "http"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._cache: dict[str, _Entry] = {}

    def cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._cache[key]
            return None
        return entry.value

    def store(self, key: str, value: Any, ttl: float) -> None:
        self._cache[key] = _Entry(value, time.monotonic() + ttl)

    def get_json(self, url: str, params: dict | None = None) -> Any | None:
        """GET and parse JSON. Returns None on any failure, having logged it."""
        try:
            response = self._client.get(url, params=params)
        except Exception as exc:
            log.warning("%s: %s unreachable (%s)", self.name, url, exc)
            return None
        if response.status_code >= 400:
            log.warning("%s: %s returned %d", self.name, url, response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            log.warning("%s: %s did not return JSON", self.name, url)
            return None

    def get_text(self, url: str, params: dict | None = None) -> str | None:
        try:
            response = self._client.get(url, params=params)
        except Exception as exc:
            log.warning("%s: %s unreachable (%s)", self.name, url, exc)
            return None
        if response.status_code >= 400:
            log.warning("%s: %s returned %d", self.name, url, response.status_code)
            return None
        return response.text

    def close(self) -> None:
        self._client.close()
