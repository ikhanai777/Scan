"""Order-book depth imbalance: real flow pressure, keyless, same venue.

The spec's fundamental leg asks for exchange netflow and whale wallet activity.
Both need a paid on-chain provider (Glassnode and peers); there is no free,
keyless source for either, and this project will not invent one. Those two
components therefore **abstain** -- see `legs/fundamental.py`.

What is available for free, from the venue already being queried, is the order
book. Depth imbalance is a different measurement from netflow but it answers a
related question honestly: right now, at prices that matter, is there more size
wanting in or wanting out?

Two design points keep it from being noise:

* **Depth is measured within a band around mid, not over the top N levels.**
  A fixed level count is meaningless across venues and tick sizes; "all resting
  size within 0.5% of mid" means the same thing everywhere.
* **A thin book abstains.** Below a minimum resting notional the imbalance is
  one participant's order, not market pressure, and a number computed from it
  would be precise and meaningless.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Resting size this far either side of mid is what "at prices that matter" means.
DEPTH_BAND = 0.005
#: Below this much total resting notional the book is too thin to read.
MIN_BOOK_NOTIONAL = 50_000.0
ORDER_BOOK_LIMIT = 100


@dataclass(frozen=True)
class BookReading:
    symbol: str
    bid_notional: float
    ask_notional: float

    @property
    def imbalance(self) -> float:
        """-1 (all offers) .. +1 (all bids). Zero is a balanced book."""
        total = self.bid_notional + self.ask_notional
        return (self.bid_notional - self.ask_notional) / total if total > 0 else 0.0

    @property
    def total_notional(self) -> float:
        return self.bid_notional + self.ask_notional


class OrderBookSource:
    """Depth imbalance via ccxt's unified order book."""

    name = "orderbook"

    def __init__(self, client: Any, band: float = DEPTH_BAND,
                 min_notional: float = MIN_BOOK_NOTIONAL) -> None:
        self._client = client
        self._band = band
        self._min_notional = min_notional

    @property
    def available(self) -> bool:
        has = getattr(self._client, "has", None) or {}
        return bool(has.get("fetchOrderBook", True))

    def read(self, symbol: str) -> BookReading | None:
        """None when the venue will not serve the book, or the book is too thin."""
        if not self.available:
            return None
        try:
            book = self._client.fetch_order_book(symbol, limit=ORDER_BOOK_LIMIT)
        except Exception as exc:
            log.debug("order book unavailable for %s: %s", symbol, exc)
            return None
        if not isinstance(book, dict):
            return None

        bids = _levels(book.get("bids"))
        asks = _levels(book.get("asks"))
        if not bids or not asks:
            return None

        best_bid, best_ask = bids[0][0], asks[0][0]
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        mid = (best_bid + best_ask) / 2.0

        floor, ceiling = mid * (1 - self._band), mid * (1 + self._band)
        bid_notional = sum(price * size for price, size in bids if price >= floor)
        ask_notional = sum(price * size for price, size in asks if price <= ceiling)

        reading = BookReading(symbol, bid_notional, ask_notional)
        if reading.total_notional < self._min_notional:
            return None
        return reading


def _levels(raw: Any) -> list[tuple[float, float]]:
    """ccxt levels are [price, size]; anything malformed is dropped, not defaulted."""
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        try:
            price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
            out.append((price, size))
    return out
