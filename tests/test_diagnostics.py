"""The live preflight, and the error messages that tell you what to change.

`doctor` is the command that answers "is this reading a real market". These
tests drive it through a stub client so every branch -- the geographic refusal,
the wrong quote currency, the unsupported timeframe, the frozen chart -- is
exercised without a network.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from support import BAR_MS, make_candles, trend_closes

from cryptosignal.diagnostics import FAIL, WARN, diagnose
from cryptosignal.exchange import CCXTFeed, FeedError, explain_exchange_error


class StubClient:
    """A ccxt-shaped venue whose every response the test controls."""

    TIMEFRAMES = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

    def __init__(self, tickers=None, markets=None, ohlcv=None, timeframes=None,
                 raise_on=None, rate_limit=50):
        self.markets = markets if markets is not None else {
            "BTC/USDT": {"base": "BTC", "quote": "USDT", "spot": True, "active": True},
            "ETH/USDT": {"base": "ETH", "quote": "USDT", "spot": True, "active": True},
        }
        self._tickers = tickers if tickers is not None else {
            "BTC/USDT": {"last": 63204.5, "quoteVolume": 9.1e8, "bid": 63204.0, "ask": 63205.0},
            "ETH/USDT": {"last": 3012.8, "quoteVolume": 4.4e8, "bid": 3012.7, "ask": 3012.9},
        }
        self._ohlcv = ohlcv
        self.timeframes = self.TIMEFRAMES if timeframes is None else timeframes
        self.raise_on = raise_on
        self.rateLimit = rate_limit

    def load_markets(self):
        if isinstance(self.raise_on, Exception):
            raise self.raise_on
        return self.markets

    def fetch_tickers(self):
        if isinstance(self.raise_on, Exception):
            raise self.raise_on
        return self._tickers

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        if self._ohlcv is None:
            raise RuntimeError("no candles configured")
        return self._ohlcv


def real_rows(count: int = 300, end_ms: int | None = None):
    """OHLCV rows shaped like a venue's, from a deterministic series."""
    candles = make_candles(trend_closes(count, drift=0.0015), end_ms=end_ms)
    return [
        [candles.timestamps[i], candles.open[i], candles.high[i],
         candles.low[i], candles.close[i], candles.volume[i]]
        for i in range(len(candles))
    ]


def feed_for(settings, **kwargs) -> CCXTFeed:
    return CCXTFeed(replace(settings, request_spacing_ms=0), client=StubClient(**kwargs))


def by_name(report):
    return {c.name: c for c in report.checks}


# ---- the happy path --------------------------------------------------------


def test_a_healthy_venue_passes_every_stage(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    assert report.ok
    names = [c.name for c in report.checks]
    assert names == ["exchange", "markets", "timeframe", "live price", "stage 1",
                     "candles", "indicators", "scoring", "rate budget", "alerts", "database"]


def test_it_reports_a_real_price_not_a_placeholder(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    detail = by_name(report)["live price"].detail
    assert "BTC/USDT" in detail        # the most liquid pair, chosen by volume
    assert "63,204" in detail or "63204" in detail
    assert "bps spread" in detail


def test_it_reports_indicator_readings_off_the_live_candles(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    detail = by_name(report)["indicators"].detail
    for reading in ("ATR", "RSI", "ADX", "rel volume"):
        assert reading in detail


def test_it_runs_the_scoring_path_on_live_data(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    detail = by_name(report)["scoring"].detail
    assert "setup" in detail and "composite" in detail


# ---- the failures people actually hit --------------------------------------


def test_an_unreachable_venue_fails_with_the_reason(settings):
    error = type("NetworkError", (Exception,), {})("connection refused")
    report = diagnose(replace(settings, database_path=":memory:"),
                      feed_for(settings, raise_on=error))

    assert not report.ok
    assert by_name(report)["exchange"].status == FAIL
    assert "Could not reach" in by_name(report)["exchange"].fix


def test_a_geographic_refusal_names_the_remedy(settings):
    report = diagnose(replace(settings, database_path=":memory:"),
                      feed_for(settings, raise_on=Exception("binance GET ... 451 restricted location")))

    fix = by_name(report)["exchange"].fix
    assert "451" in fix
    assert "CS_EXCHANGE" in fix
    assert "kraken" in fix


def test_a_wrong_quote_currency_is_caught_before_anything_else(settings):
    configured = replace(settings, quote_currency="EUR", request_spacing_ms=0,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    assert not report.ok
    assert by_name(report)["markets"].status == FAIL
    assert "CS_QUOTE" in by_name(report)["markets"].fix


def test_an_unsupported_timeframe_lists_what_the_venue_has(settings):
    configured = replace(settings, timeframe="7m", request_spacing_ms=0,
                         min_quote_volume_24h=1e6, database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    check = by_name(report)["timeframe"]
    assert check.status == FAIL
    assert "15m" in check.fix and "CS_TIMEFRAME" in check.fix


def test_a_floor_nothing_clears_says_to_lower_it(settings):
    configured = replace(settings, min_quote_volume_24h=1e12, request_spacing_ms=0,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    check = by_name(report)["stage 1"]
    assert check.status == FAIL
    assert "CS_MIN_VOLUME_24H" in check.fix


def test_a_frozen_chart_warns_rather_than_passing_silently(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    stale_end = int(time.time() * 1000) - 30 * BAR_MS
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows(end_ms=stale_end)))

    check = by_name(report)["candles"]
    assert check.status == WARN
    assert "frozen chart" in check.fix


def test_too_little_history_fails_with_the_bar_count(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows(40)))

    check = by_name(report)["indicators"]
    assert check.status == FAIL
    assert "40 bars" in check.detail


def test_a_cycle_that_cannot_finish_in_its_interval_warns(settings):
    # 21 calls at the venue's own 200ms floor is 4.2s, against a 2s cycle.
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         scan_interval_seconds=2.0, universe_size=20, database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows(), rate_limit=200))

    check = by_name(report)["rate budget"]
    assert check.status == WARN
    assert "CS_UNIVERSE_SIZE" in check.fix


def test_a_venue_rate_limit_slower_than_ours_wins(settings):
    """The budget must be computed against the venue's gap, not our own."""
    configured = replace(settings, request_spacing_ms=1, min_quote_volume_24h=1e6,
                         database_path=":memory:")
    feed = CCXTFeed(configured, client=StubClient(ohlcv=real_rows(), rate_limit=500))
    report = diagnose(configured, feed)

    assert "500ms spacing" in by_name(report)["rate budget"].detail


def test_missing_alert_channels_warn_without_blocking(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         telegram_bot_token="", webhook_url="", database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    assert by_name(report)["alerts"].status == WARN
    assert report.ok                       # a warning is not a failure


def test_configured_channels_are_named(settings):
    configured = replace(settings, request_spacing_ms=0, min_quote_volume_24h=1e6,
                         telegram_bot_token="t", telegram_chat_id="c",
                         webhook_url="https://example.invalid/h", database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))

    assert by_name(report)["alerts"].detail == "telegram, webhook"


def test_an_unknown_exchange_id_fails_at_the_first_check(settings):
    report = diagnose(replace(settings, exchange_id="not_a_venue", database_path=":memory:"))

    assert not report.ok
    assert report.checks[0].name == "exchange"
    assert "CS_EXCHANGE" in report.checks[0].fix


# ---- the rendered output ---------------------------------------------------


def test_the_report_renders_every_check_and_its_fix(settings):
    configured = replace(settings, min_quote_volume_24h=1e12, request_spacing_ms=0,
                         database_path=":memory:")
    text = diagnose(configured, feed_for(configured, ohlcv=real_rows())).render()

    assert "[ok]" in text and "[FAIL]" in text
    assert "CS_MIN_VOLUME_24H" in text


def test_a_wrapped_fix_survives_intact(settings):
    """Wrapping must not lose or reorder words -- it is advice, not decoration."""
    configured = replace(settings, min_quote_volume_24h=1e12, request_spacing_ms=0,
                         database_path=":memory:")
    report = diagnose(configured, feed_for(configured, ohlcv=real_rows()))
    fix = by_name(report)["stage 1"].fix

    flattened = " ".join(report.render().split())
    assert " ".join(fix.split()) in flattened


# ---- the error classifier --------------------------------------------------


@pytest.mark.parametrize("message, expected", [
    ("binance GET https://... 451 Service unavailable from a restricted location", "CS_EXCHANGE"),
    ("403 Forbidden", "not a credentials problem"),
    ("429 Too Many Requests", "CS_REQUEST_SPACING_MS"),
])
def test_the_classifier_maps_a_status_to_a_remedy(message, expected):
    assert expected in explain_exchange_error(Exception(message), "binance")


def test_the_classifier_falls_back_to_the_raw_error():
    explained = explain_exchange_error(ValueError("something odd"), "kraken")
    assert "kraken failed with ValueError" in explained
    assert "something odd" in explained


def test_load_markets_failures_are_explained_too(settings):
    feed = CCXTFeed(replace(settings, request_spacing_ms=0),
                    client=StubClient(raise_on=Exception("451 restricted location")))
    feed._markets_loaded = False
    with pytest.raises(FeedError, match="CS_EXCHANGE"):
        feed.snapshots()
    assert feed.stats.failures == 1
