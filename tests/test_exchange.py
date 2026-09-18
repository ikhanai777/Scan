"""The ccxt adapter, against a stub client.

These cover the parts that go wrong in production with a real venue: a ticker
missing quoteVolume, a market with no bid/ask, a symbol that 500s, and the
caching that keeps all of it inside the rate limit.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from cryptosignal.exchange import CCXTFeed, FeedError


class StubClient:
    """Enough ccxt surface for the feed: markets, tickers, OHLCV, and failures."""

    def __init__(self, tickers=None, markets=None, ohlcv=None, fail_symbols=(), fail_tickers=False):
        self.markets = markets if markets is not None else {
            "BTC/USDT": {"base": "BTC", "quote": "USDT", "spot": True, "active": True},
            "ETH/USDT": {"base": "ETH", "quote": "USDT", "spot": True, "active": True},
        }
        self._tickers = tickers if tickers is not None else {
            "BTC/USDT": {"last": 60000.0, "quoteVolume": 5e8, "bid": 59999.0, "ask": 60001.0, "percentage": 1.2},
            "ETH/USDT": {"last": 3000.0, "quoteVolume": 2e8, "bid": 2999.5, "ask": 3000.5, "percentage": -0.4},
        }
        self._ohlcv = ohlcv or []
        self.fail_symbols = set(fail_symbols)
        self.fail_tickers = fail_tickers
        self.ticker_calls = 0
        self.ohlcv_calls = 0

    def load_markets(self):
        return self.markets

    def fetch_tickers(self):
        self.ticker_calls += 1
        if self.fail_tickers:
            raise RuntimeError("exchange 503")
        return self._tickers

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.ohlcv_calls += 1
        if symbol in self.fail_symbols:
            raise RuntimeError("exchange 500")
        return self._ohlcv


def rows(count: int = 80, start: float = 100.0):
    now = int(time.time() * 1000)
    out = []
    for i in range(count):
        price = start * (1.002 ** i)
        out.append([now - (count - 1 - i) * 900_000, price, price * 1.003, price * 0.997, price, 10.0])
    return out


@pytest.fixture
def feed(settings):
    return CCXTFeed(replace(settings, request_spacing_ms=0), client=StubClient())


def test_snapshots_normalise_tickers(feed):
    snapshots = {s.symbol: s for s in feed.snapshots()}
    assert snapshots["BTC/USDT"].base == "BTC"
    assert snapshots["BTC/USDT"].quote_volume_24h == pytest.approx(5e8)
    # bid 59999 / ask 60001 is a 2-wide spread on a 60000 mid.
    assert snapshots["BTC/USDT"].spread_bps == pytest.approx(0.333, rel=0.02)


def test_base_volume_is_priced_when_quote_volume_is_missing(settings):
    client = StubClient(tickers={"BTC/USDT": {"last": 50000.0, "baseVolume": 1000.0}})
    snapshot = CCXTFeed(replace(settings, request_spacing_ms=0), client=client).snapshots()[0]
    assert snapshot.quote_volume_24h == pytest.approx(5e7)


def test_a_missing_bid_ask_yields_nan_not_zero(settings):
    """Zero would read as a perfectly tight spread and pass every filter."""
    import math

    client = StubClient(tickers={"BTC/USDT": {"last": 50000.0, "quoteVolume": 1e8}})
    snapshot = CCXTFeed(replace(settings, request_spacing_ms=0), client=client).snapshots()[0]
    assert math.isnan(snapshot.spread_bps)


def test_a_priceless_ticker_is_dropped(settings):
    client = StubClient(tickers={"BTC/USDT": {"last": None, "quoteVolume": 1e8}})
    assert CCXTFeed(replace(settings, request_spacing_ms=0), client=client).snapshots() == []


def test_non_spot_and_inactive_markets_are_dropped(settings):
    client = StubClient(markets={
        "BTC/USDT": {"base": "BTC", "quote": "USDT", "spot": True, "active": True},
        "ETH/USDT": {"base": "ETH", "quote": "USDT", "spot": False, "active": True},
        "XRP/USDT": {"base": "XRP", "quote": "USDT", "spot": True, "active": False},
    }, tickers={
        "BTC/USDT": {"last": 1.0, "quoteVolume": 1.0},
        "ETH/USDT": {"last": 1.0, "quoteVolume": 1.0},
        "XRP/USDT": {"last": 1.0, "quoteVolume": 1.0},
    })
    feed = CCXTFeed(replace(settings, request_spacing_ms=0), client=client)
    assert [s.symbol for s in feed.snapshots()] == ["BTC/USDT"]


def test_tickers_are_cached_within_the_ttl(feed):
    feed.snapshots()
    feed.snapshots()
    assert feed._client.ticker_calls == 1


def test_invalidate_forces_a_refetch(feed):
    feed.snapshots()
    feed.invalidate()
    feed.snapshots()
    assert feed._client.ticker_calls == 2


def test_a_ticker_failure_raises_and_is_counted(settings):
    feed = CCXTFeed(replace(settings, request_spacing_ms=0), client=StubClient(fail_tickers=True))
    with pytest.raises(FeedError):
        feed.snapshots()
    assert feed.stats.failures == 1


def test_candles_are_parsed_and_cached(settings):
    client = StubClient(ohlcv=rows())
    feed = CCXTFeed(replace(settings, request_spacing_ms=0), client=client)
    candles = feed.candles("BTC/USDT")

    assert len(candles) == 80
    assert candles.symbol == "BTC/USDT"
    feed.candles("BTC/USDT")
    assert client.ohlcv_calls == 1


def test_a_candle_failure_returns_none_and_is_counted(settings):
    client = StubClient(ohlcv=rows(), fail_symbols={"BTC/USDT"})
    feed = CCXTFeed(replace(settings, request_spacing_ms=0), client=client)

    assert feed.candles("BTC/USDT") is None
    assert feed.stats.failures == 1
    # A failure must not be cached -- the next cycle should try again.
    feed.candles("BTC/USDT")
    assert client.ohlcv_calls == 2


def test_an_empty_ohlcv_response_is_none(settings):
    feed = CCXTFeed(replace(settings, request_spacing_ms=0), client=StubClient(ohlcv=[]))
    assert feed.candles("BTC/USDT") is None


def test_prices_reuse_the_ticker_sweep(feed):
    prices = feed.prices(["BTC/USDT", "MISSING/USDT"])
    assert prices == {"BTC/USDT": 60000.0}
    assert feed._client.ticker_calls == 1


def test_prices_survive_a_dead_feed(settings):
    feed = CCXTFeed(replace(settings, request_spacing_ms=0), client=StubClient(fail_tickers=True))
    assert feed.prices(["BTC/USDT"]) == {}


def test_the_throttle_spaces_calls(settings):
    feed = CCXTFeed(replace(settings, request_spacing_ms=60), client=StubClient(ohlcv=rows()))
    started = time.monotonic()
    feed.candles("BTC/USDT")
    feed.invalidate()
    feed.candles("BTC/USDT")
    assert time.monotonic() - started >= 0.06


def test_an_unknown_exchange_id_is_rejected(settings):
    with pytest.raises(FeedError, match="no exchange"):
        CCXTFeed(replace(settings, exchange_id="not_a_real_exchange"))


def test_stats_reset_keeps_the_last_success(feed):
    feed.snapshots()
    last = feed.stats.last_success
    feed.stats.reset()
    assert feed.stats.attempts == 0
    assert feed.stats.last_success == last
