"""The data providers, each driven through a stub of its transport.

The point of every test here is the same: a provider with no answer must return
None, never a substituted value. A zero that means "balanced book" and a zero
that means "no book" are different facts, and the legs above depend on being
able to tell them apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from cryptosignal.sources.defillama import DefiLlamaSource
from cryptosignal.sources.derivatives import DerivativesSource
from cryptosignal.sources.fear_greed import FearGreedSource
from cryptosignal.sources.news import (
    COIN_ALIASES,
    NewsSource,
    _classify,
    _dedupe,
    _parse_time,
    decay_weight,
    parse_feed_list,
)
from cryptosignal.sources.orderbook import OrderBookSource


def fake_http(handler) -> httpx.Client:
    """An httpx client whose every request is answered by `handler(request)`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_responder(payload, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


def text_responder(body: str, status: int = 200):
    return lambda request: httpx.Response(status, text=body)


# ---- derivatives -----------------------------------------------------------


class DerivClient:
    def __init__(self, funding=None, open_interest=None, has=None, markets=None, raises=False):
        self.has = has if has is not None else {"fetchFundingRate": True, "fetchOpenInterest": True}
        self.markets = markets if markets is not None else {
            "BTC/USDT:USDT": {"swap": True, "active": True},
        }
        self._funding = funding
        self._oi = open_interest
        self.raises = raises

    def fetch_funding_rate(self, symbol):
        if self.raises:
            raise RuntimeError("venue said no")
        return {"fundingRate": self._funding}

    def fetch_open_interest(self, symbol):
        if self.raises:
            raise RuntimeError("venue said no")
        return {"openInterestValue": self._oi}


def test_derivatives_reads_funding_and_open_interest():
    source = DerivativesSource(DerivClient(funding=0.0003, open_interest=1_000_000))
    reading = source.read("BTC/USDT")
    assert reading.funding_rate == pytest.approx(0.0003)
    assert reading.open_interest == pytest.approx(1_000_000)


def test_the_first_open_interest_reading_has_no_change():
    """A change needs two observations. One is not a change of zero."""
    source = DerivativesSource(DerivClient(open_interest=1_000_000), cache_seconds=0)
    assert source.read("BTC/USDT").open_interest_change is None


def test_the_second_reading_reports_the_change():
    client = DerivClient(open_interest=1_000_000)
    source = DerivativesSource(client, cache_seconds=0)
    source.read("BTC/USDT")
    client._oi = 1_100_000
    assert source.read("BTC/USDT").open_interest_change == pytest.approx(0.1)


def test_a_pair_with_no_perp_abstains():
    source = DerivativesSource(DerivClient(markets={}))
    reading = source.read("DOGE/USDT")
    assert reading.funding_rate is None and reading.open_interest is None
    assert not reading.has_anything


def test_a_venue_without_these_endpoints_is_unavailable():
    source = DerivativesSource(DerivClient(has={}))
    assert not source.available
    assert source.read("BTC/USDT").funding_rate is None


def test_a_throwing_venue_abstains_rather_than_raising():
    source = DerivativesSource(DerivClient(funding=0.001, raises=True))
    assert source.read("BTC/USDT").funding_rate is None


def test_readings_are_cached_between_calls():
    client = DerivClient(funding=0.0002, open_interest=5.0)
    source = DerivativesSource(client, cache_seconds=60)
    first = source.read("BTC/USDT")
    client._funding = 0.9
    assert source.read("BTC/USDT") is first


# ---- order book ------------------------------------------------------------


class BookClient:
    def __init__(self, bids=None, asks=None, raises=False):
        self.has = {"fetchOrderBook": True}
        self._bids = bids
        self._asks = asks
        self.raises = raises

    def fetch_order_book(self, symbol, limit=None):
        if self.raises:
            raise RuntimeError("no book")
        return {"bids": self._bids, "asks": self._asks}


def book_side(start: float, step: float, size: float, count: int = 20):
    return [[start + i * step, size] for i in range(count)]


def test_a_bid_heavy_book_reads_positive():
    client = BookClient(bids=book_side(100.0, -0.05, 200.0), asks=book_side(100.1, 0.05, 50.0))
    reading = OrderBookSource(client, min_notional=0).read("X/USDT")
    assert reading.imbalance > 0.5


def test_an_offer_heavy_book_reads_negative():
    client = BookClient(bids=book_side(100.0, -0.05, 50.0), asks=book_side(100.1, 0.05, 200.0))
    reading = OrderBookSource(client, min_notional=0).read("X/USDT")
    assert reading.imbalance < -0.5


def test_a_balanced_book_reads_near_zero():
    client = BookClient(bids=book_side(100.0, -0.05, 100.0), asks=book_side(100.1, 0.05, 100.0))
    reading = OrderBookSource(client, min_notional=0).read("X/USDT")
    assert abs(reading.imbalance) < 0.05


def test_only_depth_near_mid_counts():
    """Size parked 5% away is not pressure on the current price."""
    near = BookClient(bids=book_side(100.0, -0.01, 100.0, 5), asks=book_side(100.1, 0.01, 100.0, 5))
    far_bids = book_side(100.0, -0.01, 100.0, 5) + [[90.0, 100_000.0]]
    far = BookClient(bids=far_bids, asks=book_side(100.1, 0.01, 100.0, 5))

    source = OrderBookSource(near, min_notional=0)
    assert source.read("X/USDT").imbalance == pytest.approx(
        OrderBookSource(far, min_notional=0).read("X/USDT").imbalance, abs=0.01
    )


def test_a_thin_book_abstains():
    client = BookClient(bids=[[100.0, 0.01]], asks=[[100.1, 0.01]])
    assert OrderBookSource(client, min_notional=50_000).read("X/USDT") is None


def test_an_empty_or_crossed_book_abstains():
    assert OrderBookSource(BookClient(bids=[], asks=[]), min_notional=0).read("X/USDT") is None
    crossed = BookClient(bids=[[101.0, 10.0]], asks=[[100.0, 10.0]])
    assert OrderBookSource(crossed, min_notional=0).read("X/USDT") is None


def test_malformed_levels_are_dropped_not_defaulted():
    client = BookClient(bids=[[100.0, 5.0], ["bad", 1.0], [None, 2.0]],
                        asks=[[100.1, 5.0]])
    reading = OrderBookSource(client, min_notional=0).read("X/USDT")
    assert reading is not None
    assert reading.bid_notional == pytest.approx(500.0)


def test_a_throwing_book_endpoint_abstains():
    assert OrderBookSource(BookClient(raises=True), min_notional=0).read("X/USDT") is None


# ---- DefiLlama -------------------------------------------------------------


LLAMA_PAYLOAD = [
    {"name": "Lido", "symbol": "LDO", "tvl": 2.5e10, "change_1d": 1.2, "change_7d": 8.4},
    {"name": "Aave", "symbol": "AAVE", "tvl": 1.1e10, "change_1d": -0.5, "change_7d": -3.1},
    {"name": "Curve", "symbol": "CRV", "tvl": 2.0e9, "change_1d": 0.1, "change_7d": 0.2},
    {"name": "Tiny", "symbol": "TINY", "tvl": 1000.0},
    {"name": "Dup A", "symbol": "DUP", "tvl": 1e9},
    {"name": "Dup B", "symbol": "DUP", "tvl": 2e9},
    {"name": "No symbol", "symbol": "-", "tvl": 1e9},
]


def test_defillama_resolves_a_protocol():
    source = DefiLlamaSource(client=fake_http(json_responder(LLAMA_PAYLOAD)))
    reading = source.read("LDO")
    assert reading.protocol == "Lido"
    assert reading.change_7d == pytest.approx(8.4)


def test_a_coin_that_is_not_a_protocol_abstains():
    source = DefiLlamaSource(client=fake_http(json_responder(LLAMA_PAYLOAD)))
    assert source.read("BTC") is None


def test_an_ambiguous_symbol_abstains_rather_than_guessing():
    """Two protocols share DUP, so neither can be the answer."""
    source = DefiLlamaSource(client=fake_http(json_responder(LLAMA_PAYLOAD)))
    assert source.read("DUP") is None


def test_a_protocol_below_the_tvl_floor_is_ignored():
    source = DefiLlamaSource(client=fake_http(json_responder(LLAMA_PAYLOAD)))
    assert source.read("TINY") is None


def test_a_dead_defillama_abstains():
    source = DefiLlamaSource(client=fake_http(json_responder({}, status=503)))
    assert source.read("LDO") is None


def test_the_protocol_list_is_fetched_once():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=LLAMA_PAYLOAD)

    source = DefiLlamaSource(client=fake_http(handler))
    source.read("LDO")
    source.read("AAVE")
    assert calls["n"] == 1


# ---- Fear & Greed ----------------------------------------------------------


FNG_PAYLOAD = {"data": [
    {"value": "82", "value_classification": "Extreme Greed", "timestamp": "1758153600"},
    {"value": "74", "value_classification": "Greed", "timestamp": "1758067200"},
]}


def test_fear_greed_reads_value_and_previous():
    reading = FearGreedSource(client=fake_http(json_responder(FNG_PAYLOAD))).read()
    assert reading.value == pytest.approx(82.0)
    assert reading.label == "Extreme Greed"
    assert reading.change == pytest.approx(8.0)


def test_fear_greed_with_one_row_has_no_change():
    payload = {"data": [FNG_PAYLOAD["data"][0]]}
    reading = FearGreedSource(client=fake_http(json_responder(payload))).read()
    assert reading.previous is None and reading.change is None


def test_an_out_of_range_value_is_rejected():
    payload = {"data": [{"value": "150", "value_classification": "Broken"}]}
    assert FearGreedSource(client=fake_http(json_responder(payload))).read() is None


def test_a_dead_fear_greed_abstains():
    assert FearGreedSource(client=fake_http(json_responder({}, status=500))).read() is None


# ---- news ------------------------------------------------------------------


def rss(items: list[tuple[str, datetime]]) -> str:
    entries = "".join(
        f"<item><title>{title}</title><link>https://example.invalid/{i}</link>"
        f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i, (title, when) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{entries}</channel></rss>'


def atom(items: list[tuple[str, datetime]]) -> str:
    entries = "".join(
        f'<entry><title>{title}</title><link href="https://example.invalid/{i}"/>'
        f"<published>{when.isoformat()}</published></entry>"
        for i, (title, when) in enumerate(items)
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


def now() -> datetime:
    return datetime.now(UTC)


def test_rss_is_parsed_into_items():
    body = rss([("Solana mainnet upgrade ships", now() - timedelta(hours=1))])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    items = source.items(48)
    assert len(items) == 1
    assert "Solana" in items[0].title


def test_atom_is_parsed_too():
    body = atom([("Ethereum upgrade goes live", now() - timedelta(hours=2))])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    assert len(source.items(48)) == 1


def test_items_outside_the_window_are_dropped():
    body = rss([
        ("Fresh solana story", now() - timedelta(hours=1)),
        ("Ancient solana story", now() - timedelta(days=30)),
    ])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    assert len(source.items(48)) == 1


def test_an_unreachable_feed_is_recorded_not_fatal():
    source = NewsSource(feeds=("https://feed.invalid/a",),
                        client=fake_http(text_responder("", status=500)))
    assert source.items(48) == []
    assert source.feed_health["https://feed.invalid/a"] == "unreachable"


def test_unparseable_xml_is_recorded_not_fatal():
    source = NewsSource(feeds=("https://feed.invalid/a",),
                        client=fake_http(text_responder("<not xml")))
    assert source.items(48) == []


def test_the_same_story_across_outlets_counts_once():
    when = now() - timedelta(hours=1)
    items = _dedupe([
        _item("Solana ETF approved", when),
        _item("Solana ETF approved", when + timedelta(minutes=5)),
    ])
    assert len(items) == 1


def _item(title, when):
    from cryptosignal.sources.news import NewsItem
    return NewsItem(title, "", when, "test")


def test_a_coin_no_headline_mentions_gets_no_reading():
    body = rss([("Bitcoin hits a record high", now() - timedelta(hours=1))])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    assert source.read("AVAX", 48, 8) is None


def test_a_mentioned_coin_gets_a_classified_reading():
    body = rss([("Coinbase lists Avalanche perpetuals", now() - timedelta(minutes=30))])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    reading = source.read("AVAX", 48, 8)
    assert reading is not None
    assert reading.impact > 50


def test_a_hack_headline_reads_bearish():
    body = rss([("Curve Finance exploited for $60M", now() - timedelta(minutes=15))])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    reading = source.read("CRV", 48, 8)
    assert reading.impact < -50


def test_recency_outweighs_staleness():
    """Two opposite events: the fresher one has to win."""
    body = rss([
        ("Solana partnership announced", now() - timedelta(hours=40)),
        ("Solana network outage halts trading", now() - timedelta(minutes=10)),
    ])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    assert source.read("SOL", 48, 8).impact < 0


def test_decay_halves_at_the_half_life():
    assert decay_weight(8.0, 8.0) == pytest.approx(0.5)
    assert decay_weight(16.0, 8.0) == pytest.approx(0.25)
    assert decay_weight(0.0, 8.0) == pytest.approx(1.0)


def test_decay_rejects_a_nonsense_half_life():
    with pytest.raises(ValueError):
        decay_weight(1.0, 0.0)


def test_an_unclassified_headline_scores_zero():
    assert _classify("Solana price moves sideways on Tuesday") == 0.0


def test_a_headline_the_lexicon_cannot_call_scores_zero():
    """A listing and an exploit in one headline is not a mild buy."""
    assert _classify("Coinbase lists token days after exploit drained the treasury") == 0.0


def test_a_clear_winner_still_scores_when_both_lexicons_hit():
    """Ambiguity is a near-tie, not any co-occurrence."""
    score = _classify("Network outage resolved as Coinbase lists the token")
    assert score > 30


def test_short_tickers_do_not_match_english_words():
    """'ONE' and 'SUN' are tickers; matching them as words would be noise."""
    body = rss([("One more reason the sun will rise on gas fees", now() - timedelta(hours=1))])
    source = NewsSource(feeds=("https://feed.invalid/a",), client=fake_http(text_responder(body)))
    for ticker in ("ONE", "SUN", "GAS"):
        assert source.read(ticker, 48, 8) is None


def test_major_coins_have_name_aliases():
    for symbol in ("BTC", "ETH", "SOL", "AVAX"):
        assert symbol in COIN_ALIASES


def test_rfc822_and_iso_dates_both_parse():
    assert _parse_time("Thu, 18 Sep 2026 12:00:00 +0000") is not None
    assert _parse_time("2026-09-18T12:00:00Z") is not None
    assert _parse_time("not a date") is None
    assert _parse_time(None) is None


def test_feed_list_override_falls_back_to_defaults():
    assert parse_feed_list("") == NewsSource().feeds
    assert parse_feed_list("https://a.invalid/f, https://b.invalid/f") == (
        "https://a.invalid/f", "https://b.invalid/f")
