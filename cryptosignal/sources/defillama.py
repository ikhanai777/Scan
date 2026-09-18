"""TVL and protocol-usage trend, from DefiLlama's free public API.

The spec asks for "TVL or protocol-usage trend for DeFi tokens". DefiLlama
serves exactly that, keyless and without a signup, at api.llama.fi.

Two honest limits, both handled by abstaining rather than guessing:

* **Most coins are not protocols.** BTC, SOL, DOGE have no TVL. A coin with no
  matching protocol reports None, and the fundamental leg renormalises. It
  would be easy and wrong to score those zero.
* **Symbol matching is imperfect.** DefiLlama keys protocols by its own slug
  and lists a `symbol` field that is sometimes blank or shared. Only an exact,
  unambiguous symbol match is used; an ambiguous one abstains.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from . import HTTPSource

log = logging.getLogger(__name__)

PROTOCOLS_URL = "https://api.llama.fi/protocols"
# The whole protocol list is one large response that changes slowly; an hour
# of cache keeps a 2-minute scan cycle from hammering a free service.
CACHE_SECONDS = 3600.0
MIN_TVL_USD = 5_000_000.0


@dataclass(frozen=True)
class TVLReading:
    symbol: str
    protocol: str
    tvl_usd: float
    change_1d: float | None      # percent
    change_7d: float | None      # percent


class DefiLlamaSource(HTTPSource):
    name = "defillama"

    def _protocols(self) -> dict[str, dict] | None:
        """Symbol -> protocol, for symbols that map to exactly one protocol."""
        cached = self.cached("protocols")
        if cached is not None:
            return cached

        payload = self.get_json(PROTOCOLS_URL)
        if not isinstance(payload, list):
            return None

        # A symbol shared by several protocols cannot be resolved, so it is
        # dropped entirely rather than resolved to whichever came first.
        seen: dict[str, dict] = {}
        ambiguous: set[str] = set()
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol") or "").strip().upper()
            if not symbol or symbol == "-":
                continue
            tvl = _number(entry.get("tvl"))
            if tvl is None or tvl < MIN_TVL_USD:
                continue
            if symbol in seen:
                ambiguous.add(symbol)
                continue
            seen[symbol] = entry

        for symbol in ambiguous:
            seen.pop(symbol, None)

        self.store("protocols", seen, CACHE_SECONDS)
        log.info("defillama: %d protocols resolved to a unique symbol", len(seen))
        return seen

    def read(self, base_symbol: str) -> TVLReading | None:
        """TVL for a coin, or None when it simply is not a protocol."""
        protocols = self._protocols()
        if not protocols:
            return None
        entry = protocols.get(base_symbol.strip().upper())
        if entry is None:
            return None

        tvl = _number(entry.get("tvl"))
        if tvl is None:
            return None
        return TVLReading(
            symbol=base_symbol.upper(),
            protocol=str(entry.get("name") or entry.get("slug") or "unknown"),
            tvl_usd=tvl,
            change_1d=_number(entry.get("change_1d")),
            change_7d=_number(entry.get("change_7d")),
        )


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None
