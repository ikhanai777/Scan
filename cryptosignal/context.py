"""Everything the non-technical legs need, gathered once per cycle.

The fundamental and sentiment legs read from several providers, and most of
those answers are either market-wide (Fear & Greed, the whole headline set) or
change far more slowly than a scan cycle. Fetching them per candidate would be
wasteful and, on a free API, rude.

So `MarketContext` fetches the shared parts once at the top of a cycle and then
answers per coin from what it already has. A provider that fails leaves its
slot empty; nothing here substitutes a value, and `sources_reporting` is what
the dashboard and `doctor` use to say which providers actually answered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .sources.defillama import DefiLlamaSource, TVLReading
from .sources.derivatives import DerivativesReading, DerivativesSource
from .sources.fear_greed import FearGreedSource, RegimeReading
from .sources.news import NewsReading, NewsSource, parse_feed_list
from .sources.orderbook import BookReading, OrderBookSource

log = logging.getLogger(__name__)


@dataclass
class CoinContext:
    """The per-coin slice handed to the legs. Any field may be None."""

    derivatives: DerivativesReading | None = None
    book: BookReading | None = None
    tvl: TVLReading | None = None
    news: NewsReading | None = None


class MarketContext:
    """Owns the non-price providers for one scan cycle."""

    def __init__(self, settings: Settings, exchange_client: Any | None = None,
                 sources: dict[str, Any] | None = None) -> None:
        self.settings = settings
        sources = sources or {}

        # The venue-backed sources need the live ccxt client; without one they
        # are simply absent, which is how the technical-only path still runs.
        self.derivatives: DerivativesSource | None = sources.get("derivatives")
        self.orderbook: OrderBookSource | None = sources.get("orderbook")
        if exchange_client is not None:
            self.derivatives = self.derivatives or DerivativesSource(exchange_client)
            self.orderbook = self.orderbook or OrderBookSource(exchange_client)

        self.defillama: DefiLlamaSource | None = sources.get("defillama")
        self.news: NewsSource | None = sources.get("news")
        self.fear_greed: FearGreedSource | None = sources.get("fear_greed")

        if settings.enable_fundamental_leg and self.defillama is None:
            self.defillama = DefiLlamaSource()
        if settings.enable_sentiment_leg:
            if self.news is None:
                import os

                self.news = NewsSource(
                    feeds=parse_feed_list(os.environ.get("CS_NEWS_FEEDS", "")),
                    cryptopanic_token=settings.cryptopanic_token,
                )
            if self.fear_greed is None:
                self.fear_greed = FearGreedSource()

        self.regime: RegimeReading | None = None
        self.sources_reporting: dict[str, bool] = {}

    # -- once per cycle ---------------------------------------------------

    def refresh(self) -> None:
        """Pull the market-wide parts. Called once, at the top of a cycle."""
        self.sources_reporting = {}

        if self.settings.enable_sentiment_leg and self.fear_greed is not None:
            self.regime = self.fear_greed.read()
            self.sources_reporting["fear_greed"] = self.regime is not None

        if self.settings.enable_sentiment_leg and self.news is not None:
            headlines = self.news.items(self.settings.news_window_hours)
            self.sources_reporting["news"] = bool(headlines)
            if headlines:
                log.info("news: %d headlines across %d feed(s)",
                         len(headlines), len(self.news.feed_health))

        if self.settings.enable_fundamental_leg:
            if self.defillama is not None:
                self.sources_reporting["defillama"] = self.defillama.read("ETH") is not None
            if self.derivatives is not None:
                self.sources_reporting["derivatives"] = self.derivatives.available
            if self.orderbook is not None:
                self.sources_reporting["orderbook"] = self.orderbook.available

    # -- per candidate ----------------------------------------------------

    def for_coin(self, symbol: str, base: str) -> CoinContext:
        context = CoinContext()

        if self.settings.enable_fundamental_leg:
            if self.derivatives is not None:
                context.derivatives = self.derivatives.read(symbol)
            if self.orderbook is not None:
                context.book = self.orderbook.read(symbol)
            if self.defillama is not None:
                context.tvl = self.defillama.read(base)

        if self.settings.enable_sentiment_leg and self.news is not None:
            context.news = self.news.read(
                base, self.settings.news_window_hours, self.settings.news_half_life_hours,
            )
        return context

    def close(self) -> None:
        for source in (self.defillama, self.news, self.fear_greed):
            if source is not None:
                source.close()
