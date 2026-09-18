"""Ingestion: one exchange, normalised to the common schema, politely.

Phase 1 is a single venue via ccxt. Everything the rest of the app sees is a
`MarketSnapshot` or a `Candles`, so adding a second venue later is a new
implementation of `Feed`, not a change to the scoring path.

Two things this layer owes the provider and the app: it stays inside the rate
limit (ccxt's own throttle, plus a courtesy gap and per-call caches), and it
reports its own failures honestly so the scanner's kill-switch has something
to act on.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings
from .models import Candles, MarketSnapshot, timeframe_minutes

log = logging.getLogger(__name__)


class FeedError(RuntimeError):
    """A fetch failed in a way the caller should count, not crash on."""


class Feed(Protocol):
    """What the scanner needs from a data source. Fakes in tests implement this."""

    def snapshots(self) -> list[MarketSnapshot]: ...
    def candles(self, symbol: str) -> Candles | None: ...
    def prices(self, symbols: list[str]) -> dict[str, float]: ...


@dataclass
class FetchStats:
    attempts: int = 0
    failures: int = 0
    last_success: float | None = None

    def record(self, ok: bool) -> None:
        self.attempts += 1
        if ok:
            self.last_success = time.time()
        else:
            self.failures += 1

    def reset(self) -> None:
        self.attempts = 0
        self.failures = 0

    @property
    def failure_ratio(self) -> float:
        return self.failures / self.attempts if self.attempts else 0.0


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class _TTLCache:
    """Small TTL cache. The point is the rate limit, not the microseconds."""

    def __init__(self) -> None:
        self._data: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._data[key]
            return None
        return entry.value

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = _CacheEntry(value, time.monotonic() + ttl)

    def clear(self) -> None:
        self._data.clear()


class CCXTFeed:
    """A live ccxt spot feed for one exchange."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.stats = FetchStats()
        self._cache = _TTLCache()
        self._last_call = 0.0
        self._client = client if client is not None else self._build_client(settings)
        self._markets_loaded = client is not None

    @staticmethod
    def _build_client(settings: Settings) -> Any:
        import ccxt  # imported here so the scoring path never needs it installed

        if not hasattr(ccxt, settings.exchange_id):
            raise FeedError(f"ccxt has no exchange {settings.exchange_id!r}")
        factory = getattr(ccxt, settings.exchange_id)
        return factory({"enableRateLimit": True, "options": {"defaultType": "spot"}})

    # -- plumbing ---------------------------------------------------------

    def _throttle(self) -> None:
        gap = self.settings.request_spacing_ms / 1000.0
        elapsed = time.monotonic() - self._last_call
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_call = time.monotonic()

    def _ensure_markets(self) -> None:
        if self._markets_loaded:
            return
        self._throttle()
        self._client.load_markets()
        self._markets_loaded = True

    # -- Feed -------------------------------------------------------------

    def snapshots(self) -> list[MarketSnapshot]:
        """Every spot market on the venue, as liquidity + cost of entry."""
        cached = self._cache.get("tickers")
        if cached is not None:
            return cached

        self._ensure_markets()
        self._throttle()
        try:
            tickers = self._client.fetch_tickers()
        except Exception as exc:                     # ccxt raises a wide family
            self.stats.record(ok=False)
            raise FeedError(f"fetch_tickers failed: {exc}") from exc
        self.stats.record(ok=True)

        markets = getattr(self._client, "markets", {}) or {}
        out: list[MarketSnapshot] = []
        for symbol, ticker in tickers.items():
            market = markets.get(symbol)
            if market is None or not market.get("spot", True) or market.get("active") is False:
                continue
            snapshot = self._to_snapshot(symbol, market, ticker)
            if snapshot is not None:
                out.append(snapshot)

        self._cache.put("tickers", out, self.settings.ticker_cache_seconds)
        return out

    @staticmethod
    def _to_snapshot(symbol: str, market: dict, ticker: dict) -> MarketSnapshot | None:
        last = _number(ticker.get("last") or ticker.get("close"))
        if not math.isfinite(last) or last <= 0:
            return None

        quote_volume = _number(ticker.get("quoteVolume"))
        if not math.isfinite(quote_volume):
            # Some venues only report base volume; price it ourselves.
            base_volume = _number(ticker.get("baseVolume"))
            quote_volume = base_volume * last if math.isfinite(base_volume) else float("nan")

        bid, ask = _number(ticker.get("bid")), _number(ticker.get("ask"))
        if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            spread_bps = (ask - bid) / mid * 10_000.0
        else:
            spread_bps = float("nan")

        return MarketSnapshot(
            symbol=symbol,
            base=str(market.get("base", "")).upper(),
            quote=str(market.get("quote", "")).upper(),
            last=last,
            quote_volume_24h=quote_volume,
            spread_bps=spread_bps,
            change_24h_pct=_number(ticker.get("percentage")),
        )

    def candles(self, symbol: str) -> Candles | None:
        """OHLCV for one symbol. Returns None on a failure the caller should count."""
        key = f"ohlcv:{symbol}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        self._ensure_markets()
        self._throttle()
        try:
            rows = self._client.fetch_ohlcv(
                symbol, timeframe=self.settings.timeframe, limit=self.settings.ohlcv_limit
            )
        except Exception as exc:
            self.stats.record(ok=False)
            log.warning("ohlcv fetch failed for %s: %s", symbol, exc)
            return None
        self.stats.record(ok=True)

        candles = Candles.from_rows(symbol, self.settings.timeframe, rows or [])
        if len(candles) == 0:
            return None
        self._cache.put(key, candles, self.settings.ohlcv_cache_seconds)
        return candles

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """Last price for the given symbols, reusing the cached ticker sweep."""
        if not symbols:
            return {}
        wanted = set(symbols)
        try:
            return {s.symbol: s.last for s in self.snapshots() if s.symbol in wanted}
        except FeedError as exc:
            log.warning("price refresh failed: %s", exc)
            return {}

    def invalidate(self) -> None:
        self._cache.clear()


def _number(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def candles_are_stale(candles: Candles, settings: Settings) -> bool:
    """True when the venue has stopped publishing this market.

    A signal computed from a frozen chart is worse than no signal, so the
    scanner drops these before they reach scoring rather than after.
    """
    limit_seconds = timeframe_minutes(settings.timeframe) * 60 * settings.max_candle_age_multiple
    return candles.age_seconds() > limit_seconds
