"""Funding rate and open interest, from the same venue as the price.

The spec's fundamental leg wants funding and open interest, and both are the
cheapest real data in this whole project: they come from the exchange over
ccxt, keyless, on the perpetual contract that shadows each spot pair. No third
party, no signup, no key.

What they mean, and why the sign is not what a beginner expects:

* **Funding rate** is what longs pay shorts (positive) or shorts pay longs
  (negative) to hold a perp. Extreme positive funding means the crowd is
  levered long and paying for the privilege -- crowded, and the side that gets
  liquidated first. So this leg reads extreme funding as a *contrarian* signal,
  which is the spec's "extreme funding flags reversal risk". Mild funding in
  line with the trend is confirmation; extreme funding against it is a warning.

* **Open interest** is the size of the outstanding position book. Rising OI
  with rising price is new money backing the move; rising OI with falling price
  is new shorts. Falling OI is positions closing -- the move is being unwound
  rather than joined. OI alone has no direction; it only qualifies price.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Funding is normally a few basis points per interval. Past this the crowd is
# paying real money to stay in, which is the crowded-trade reading.
FUNDING_EXTREME = 0.0005        # 0.05% per interval, ~55% annualised at 8h
FUNDING_ELEVATED = 0.0002
CACHE_SECONDS = 120.0


@dataclass(frozen=True)
class DerivativesReading:
    """What the venue says about the perp behind a spot symbol."""

    symbol: str
    funding_rate: float | None          # per interval, as a fraction
    open_interest: float | None         # in contracts or base units, venue-defined
    open_interest_change: float | None  # fraction vs the previous reading we saw

    @property
    def has_anything(self) -> bool:
        return self.funding_rate is not None or self.open_interest is not None


class DerivativesSource:
    """Reads funding and OI through ccxt, and abstains where the venue has none.

    Not every spot pair has a perp, and not every venue exposes these over
    ccxt's unified API. Both are normal: the source returns None for what it
    cannot see, and the leg renormalises.
    """

    name = "derivatives"

    def __init__(self, client: Any, cache_seconds: float = CACHE_SECONDS) -> None:
        self._client = client
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, DerivativesReading]] = {}
        # Open interest is only meaningful as a change, so the previous reading
        # is kept per symbol. An empty history means the first cycle reports the
        # level with no change -- not a fabricated zero.
        self._previous_oi: dict[str, float] = {}
        self._supports_funding = self._has("fetchFundingRate")
        self._supports_oi = self._has("fetchOpenInterest")

    def _has(self, capability: str) -> bool:
        has = getattr(self._client, "has", None) or {}
        return bool(has.get(capability))

    @property
    def available(self) -> bool:
        """False when this venue exposes neither reading -- say so once, up front."""
        return self._supports_funding or self._supports_oi

    def perp_symbol(self, spot_symbol: str) -> str | None:
        """The perpetual that shadows a spot pair, if the venue lists one.

        ccxt spells these `BASE/QUOTE:SETTLE` (BTC/USDT:USDT). Resolving it
        through the loaded market list rather than string-building means a
        venue with a different convention simply reports no perp instead of
        sending a request that 404s every cycle.
        """
        markets = getattr(self._client, "markets", None) or {}
        base, _, quote = spot_symbol.partition("/")
        if not quote:
            return None
        for candidate in (f"{base}/{quote}:{quote}", f"{base}/USD:{base}", f"{base}/USDT:USDT"):
            market = markets.get(candidate)
            if market and market.get("swap") and market.get("active") is not False:
                return candidate
        return None

    def read(self, spot_symbol: str) -> DerivativesReading:
        cached = self._cache.get(spot_symbol)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        perp = self.perp_symbol(spot_symbol) if self.available else None
        if perp is None:
            reading = DerivativesReading(spot_symbol, None, None, None)
            self._cache[spot_symbol] = (time.monotonic() + self._cache_seconds, reading)
            return reading

        funding = self._funding(perp)
        open_interest = self._open_interest(perp)

        change = None
        if open_interest is not None:
            previous = self._previous_oi.get(perp)
            if previous and previous > 0:
                change = (open_interest - previous) / previous
            self._previous_oi[perp] = open_interest

        reading = DerivativesReading(spot_symbol, funding, open_interest, change)
        self._cache[spot_symbol] = (time.monotonic() + self._cache_seconds, reading)
        return reading

    def _funding(self, perp: str) -> float | None:
        if not self._supports_funding:
            return None
        try:
            payload = self._client.fetch_funding_rate(perp)
        except Exception as exc:
            log.debug("funding unavailable for %s: %s", perp, exc)
            return None
        rate = _number(payload.get("fundingRate") if isinstance(payload, dict) else None)
        return rate if rate is not None else None

    def _open_interest(self, perp: str) -> float | None:
        if not self._supports_oi:
            return None
        try:
            payload = self._client.fetch_open_interest(perp)
        except Exception as exc:
            log.debug("open interest unavailable for %s: %s", perp, exc)
            return None
        if not isinstance(payload, dict):
            return None
        for key in ("openInterestValue", "openInterestAmount", "openInterest"):
            value = _number(payload.get(key))
            if value is not None and value > 0:
                return value
        return None


def _number(value: Any) -> float | None:
    """Parse to float, or None. Never a substituted zero."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None
