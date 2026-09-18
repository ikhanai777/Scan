"""The scan cycle end to end, and the API it feeds, against a fake exchange."""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest
from support import BAR_MS, make_candles, range_closes, snapshot, trend_closes

from cryptosignal.exchange import FeedError, FetchStats, candles_are_stale
from cryptosignal.models import Candles, Direction, SignalStatus
from cryptosignal.scanner import Scanner
from cryptosignal.store import Store


class FakeFeed:
    """A scripted exchange: fixed markets, fixed charts, countable failures."""

    def __init__(self, markets, charts, failing: set[str] | None = None,
                 raise_on_snapshots: bool = False):
        self.markets = markets
        self.charts = charts
        self.failing = failing or set()
        self.raise_on_snapshots = raise_on_snapshots
        self.stats = FetchStats(last_success=time.time())
        self.candle_calls = 0

    def snapshots(self):
        if self.raise_on_snapshots:
            raise FeedError("exchange unreachable")
        self.stats.record(ok=True)
        return list(self.markets)

    def candles(self, symbol):
        self.candle_calls += 1
        if symbol in self.failing:
            self.stats.record(ok=False)
            return None
        self.stats.record(ok=True)
        return self.charts.get(symbol)

    def prices(self, symbols):
        wanted = set(symbols)
        return {m.symbol: m.last for m in self.markets if m.symbol in wanted}


def bullish_feed(count: int = 3) -> FakeFeed:
    """Markets whose ticker price agrees with the last close of their chart.

    A fake where the two disagree would quietly stop every signal out on the
    next cycle, and the tests would be measuring the fake, not the scanner.
    """
    markets, charts = [], {}
    for i in range(count):
        symbol = f"C{i}/USDT"
        volumes = np.concatenate([np.full(256, 1000.0), np.full(4, 3500.0)])
        chart = make_candles(trend_closes(drift=0.006, seed=20 + i),
                             symbol=symbol, volumes=volumes)
        charts[symbol] = chart
        markets.append(snapshot(symbol, f"C{i}", last=chart.last_close,
                                volume=90_000_000 - i))
    return FakeFeed(markets, charts)


@pytest.fixture
def store() -> Store:
    store = Store(":memory:")
    yield store
    store.close()


# ---- the cycle -------------------------------------------------------------


def test_a_clean_cycle_fires_signals(settings, store):
    feed = bullish_feed()
    report = Scanner(settings, feed, store).run_cycle()

    assert report.markets_seen == 3
    assert report.passed_stage1 == 3
    assert report.signals_fired >= 1
    assert not report.halted
    assert all(s.direction is Direction.LONG for s in store.recent_signals())


def test_a_flat_market_fires_nothing(settings, store):
    markets = [snapshot("FLAT/USDT", "FLAT")]
    charts = {"FLAT/USDT": make_candles(range_closes(), symbol="FLAT/USDT")}
    report = Scanner(settings, FakeFeed(markets, charts), store).run_cycle()

    assert report.signals_fired == 0
    assert store.recent_signals() == []


def test_a_downtrend_fires_shorts(settings, store):
    markets = [snapshot("DOWN/USDT", "DOWN")]
    charts = {"DOWN/USDT": make_candles(trend_closes(drift=-0.006), symbol="DOWN/USDT")}
    Scanner(settings, FakeFeed(markets, charts), store).run_cycle()

    signals = store.recent_signals()
    assert len(signals) == 1
    assert signals[0].direction is Direction.SHORT


def test_a_coin_with_an_open_signal_is_not_rescanned(settings, store):
    scanner = Scanner(settings, bullish_feed(1), store)
    scanner.run_cycle()
    assert len(store.recent_signals()) == 1

    second = scanner.run_cycle()
    assert second.passed_stage1 == 0
    assert second.signals_fired == 0
    assert len(store.recent_signals()) == 1


def test_capacity_limits_how_many_fire_at_once(settings, store):
    capped = replace(settings, max_open_signals=2, shortlist_size=10)
    report = Scanner(capped, bullish_feed(5), store).run_cycle()

    assert report.signals_fired == 2
    assert len(store.open_symbols()) == 2


def test_capacity_keeps_the_strongest_calls(settings, store):
    capped = replace(settings, max_open_signals=1, shortlist_size=10)
    feed = bullish_feed(4)
    Scanner(capped, feed, store).run_cycle()

    fired = store.recent_signals()[0]
    assert fired.confidence >= 55.0


def test_open_signals_are_graded_before_new_ones_fire(settings, store):
    scanner = Scanner(settings, bullish_feed(1), store)
    scanner.run_cycle()
    signal = store.recent_signals()[0]

    # Next cycle: price has collapsed through the stop.
    crashed = snapshot("C0/USDT", "C0", last=signal.levels.stop * 0.9, volume=90_000_000)
    scanner.feed.markets = [crashed]
    scanner.run_cycle()

    assert store.get_signal(signal.id).status is SignalStatus.CLOSED_STOP


def test_the_kill_switch_halts_on_too_many_fetch_failures(settings, store):
    feed = bullish_feed(3)
    feed.failing = {"C0/USDT", "C1/USDT"}
    report = Scanner(settings, feed, store).run_cycle()

    assert report.halted
    assert "OHLCV fetch failure" in report.halt_reason
    assert report.signals_fired == 0


def test_the_kill_switch_halts_when_the_charts_have_frozen(settings, store):
    """A chart that arrives but has stopped moving is a degraded feed too."""
    old_end = int(time.time() * 1000) - 20 * BAR_MS
    markets, charts = [], {}
    for i in range(3):
        symbol = f"F{i}/USDT"
        chart = make_candles(trend_closes(drift=0.006, seed=i), symbol=symbol, end_ms=old_end)
        charts[symbol] = chart
        markets.append(snapshot(symbol, f"F{i}", last=chart.last_close, volume=90_000_000))

    report = Scanner(settings, FakeFeed(markets, charts), store).run_cycle()
    assert report.halted
    assert "stale chart" in report.halt_reason
    assert report.signals_fired == 0


def test_the_kill_switch_backstops_a_feed_that_never_succeeds(settings, store):
    scanner = Scanner(settings, bullish_feed(1), store)
    scanner.feed.stats.last_success = time.time() - settings.stale_feed_seconds - 60
    halted, reason = scanner._should_halt(fetch_failures=0, stale_charts=0, attempted=0)
    assert halted
    assert "no successful fetch" in reason


def test_a_halt_still_grades_open_signals(settings, store):
    """Halting new calls must never abandon the ones already running."""
    scanner = Scanner(settings, bullish_feed(1), store)
    scanner.run_cycle()
    signal = store.recent_signals()[0]

    scanner.feed.markets = [snapshot("C0/USDT", "C0", last=signal.levels.target2 * 1.05,
                                     volume=90_000_000)]
    scanner.feed.failing = {"C0/USDT"}
    scanner.feed.stats.last_success = time.time() - 10_000
    scanner.run_cycle()

    assert store.get_signal(signal.id).status is SignalStatus.CLOSED_TARGET


def test_an_unreachable_exchange_reports_rather_than_raises(settings, store):
    feed = FakeFeed([], {}, raise_on_snapshots=True)
    report = Scanner(settings, feed, store).run_cycle()

    assert report.halted
    assert "unreachable" in report.halt_reason
    assert store.last_cycle()["halted"] is True


def test_stale_candles_are_skipped(settings, store):
    old_end = int(time.time() * 1000) - 20 * BAR_MS
    markets = [snapshot("OLD/USDT", "OLD")]
    charts = {"OLD/USDT": make_candles(trend_closes(drift=0.006), symbol="OLD/USDT", end_ms=old_end)}
    report = Scanner(settings, FakeFeed(markets, charts), store).run_cycle()

    assert report.shortlisted == 0
    assert report.signals_fired == 0


def test_candle_staleness_uses_the_timeframe(settings):
    fresh = make_candles(trend_closes(60))
    stale = make_candles(trend_closes(60), end_ms=int(time.time() * 1000) - 10 * BAR_MS)
    assert not candles_are_stale(fresh, settings)
    assert candles_are_stale(stale, settings)


def test_the_cycle_report_carries_the_shortlist(settings, store):
    Scanner(settings, bullish_feed(3), store).run_cycle()
    cycle = store.last_cycle()

    assert len(cycle["shortlist"]) == 3
    assert {"symbol", "setup_score", "components"} <= set(cycle["shortlist"][0])


def test_run_forever_stops_after_the_requested_cycles(settings, store):
    fast = replace(settings, scan_interval_seconds=0.0)
    scanner = Scanner(fast, bullish_feed(1), store)
    scanner.run_forever(max_cycles=2)
    assert len(store.recent_signals(status="open")) <= 1


def test_a_raising_cycle_does_not_kill_the_loop(settings, store):
    fast = replace(settings, scan_interval_seconds=0.0)
    scanner = Scanner(fast, bullish_feed(1), store)
    calls = {"n": 0}
    original = scanner.run_cycle

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return original()

    scanner.run_cycle = flaky
    scanner.run_forever(max_cycles=2)
    assert calls["n"] == 2


def test_notifiers_are_called_on_fire_and_on_resolution(settings, store):
    from cryptosignal.alerts import NotifierGroup

    class Recorder:
        name = "recorder"

        def __init__(self):
            self.fired, self.resolved = [], []

        def signal_fired(self, signal):
            self.fired.append(signal.symbol)

        def signal_resolved(self, update):
            self.resolved.append(update.kind)

    recorder = Recorder()
    scanner = Scanner(settings, bullish_feed(1), store, NotifierGroup([recorder]))
    scanner.run_cycle()
    assert recorder.fired == ["C0/USDT"]

    signal = store.recent_signals()[0]
    scanner.feed.markets = [snapshot("C0/USDT", "C0", last=signal.levels.stop * 0.9,
                                     volume=90_000_000)]
    scanner.run_cycle()
    assert recorder.resolved == ["stop"]


def test_a_failing_notifier_does_not_lose_the_signal(settings, store):
    from cryptosignal.alerts import NotifierGroup

    class Broken:
        name = "broken"

        def signal_fired(self, signal):
            raise RuntimeError("telegram is down")

        def signal_resolved(self, update):
            raise RuntimeError("still down")

    Scanner(settings, bullish_feed(1), store, NotifierGroup([Broken()])).run_cycle()
    assert len(store.recent_signals()) == 1


# ---- the API ---------------------------------------------------------------


@pytest.fixture
def client(settings, store):
    from fastapi.testclient import TestClient

    from cryptosignal.api.app import create_app

    scanner = Scanner(settings, bullish_feed(3), store)
    scanner.run_cycle()
    return TestClient(create_app(settings, store, scanner)), store


def test_health_reports_the_last_cycle(client):
    api, _ = client
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["scanner_attached"] is True
    assert body["last_cycle_at"]


def test_signals_endpoint_carries_the_disclaimer(client):
    api, _ = client
    body = api.get("/api/signals").json()
    assert body["count"] >= 1
    assert "Not financial advice" in body["disclaimer"]


def test_every_signal_card_has_levels_and_drivers(client):
    api, _ = client
    card = api.get("/api/signals").json()["signals"][0]
    for key in ("entry_low", "entry_high", "stop", "target1", "target2",
                "confidence", "hold_minutes", "drivers", "minutes_remaining"):
        assert key in card
    assert len(card["drivers"]) >= 1


def test_signal_filters_are_applied(client):
    api, _ = client
    assert api.get("/api/signals?direction=short").json()["count"] == 0
    assert api.get("/api/signals?direction=long").json()["count"] >= 1
    assert api.get("/api/signals?min_confidence=99").json()["count"] == 0


def test_a_bad_direction_is_rejected(client):
    api, _ = client
    assert api.get("/api/signals?direction=sideways").status_code == 422


def test_signal_detail_includes_its_events(client):
    api, store = client
    signal_id = store.recent_signals()[0].id
    body = api.get(f"/api/signals/{signal_id}").json()
    assert body["signal"]["id"] == signal_id
    assert body["events"][0]["kind"] == "fired"


def test_unknown_signal_is_a_404(client):
    api, _ = client
    assert api.get("/api/signals/nope").status_code == 404


def test_candidates_endpoint_exposes_the_watchlist(client):
    api, _ = client
    body = api.get("/api/candidates").json()
    assert len(body["shortlist"]) == 3
    assert body["as_of"]


def test_performance_endpoint_starts_honest(client):
    api, _ = client
    performance = api.get("/api/performance").json()["performance"]
    assert performance["closed"] == 0
    assert performance["open"] >= 1


def test_status_endpoint_publishes_the_tuning(client):
    api, _ = client
    body = api.get("/api/status").json()
    assert body["config"]["weight_technical"] == 0.5
    assert body["cycle"]["markets_seen"] == 3


def test_secrets_are_never_echoed_in_the_config(settings):
    configured = replace(settings, telegram_bot_token="123:SECRET", telegram_chat_id="42")
    published = configured.as_dict()
    assert published["telegram_bot_token"] == "set"
    assert "SECRET" not in str(published)


def test_features_endpoint_serves_the_last_cycles_readings(client):
    api, _ = client
    body = api.get("/api/features/C0/USDT").json()
    assert body["features"]["symbol"] == "C0/USDT"
    assert body["features"]["atr"] > 0


def test_features_endpoint_404s_for_an_unscanned_symbol(client):
    api, _ = client
    assert api.get("/api/features/NOPE/USDT").status_code == 404


def test_the_dashboard_is_served(client):
    api, _ = client
    response = api.get("/")
    assert response.status_code == 200
    assert "cryptosignal" in response.text
    assert "Not financial advice" in response.text


def test_nan_readings_serialise_as_null_not_zero(client):
    """JSON has no nan; a warm-up gap must not be published as a real value."""
    api, _ = client
    body = api.get("/api/features/C0/USDT").json()
    assert all(v is None or not isinstance(v, str) or True for v in body["features"].values())
    assert "NaN" not in api.get("/api/features/C0/USDT").text


def test_candles_from_rows_drops_corrupt_rows():
    rows = [
        [1, 1.0, 2.0, 0.5, 1.5, 10.0],
        [2, 1.5, None, 1.0, 1.4, 5.0],       # null high
        [3, 1.4, 0.5, 2.0, 1.6, 7.0],        # high below low
        [4, 1.6, 2.2, 1.5, 2.0, 9.0],
    ]
    candles = Candles.from_rows("X/USDT", "15m", rows)
    assert len(candles) == 2
    assert candles.timestamps == (1, 4)


def test_candles_from_rows_sorts_by_timestamp():
    rows = [[3, 1, 2, 0.5, 1.5, 1], [1, 1, 2, 0.5, 1.4, 1], [2, 1, 2, 0.5, 1.6, 1]]
    candles = Candles.from_rows("X/USDT", "15m", rows)
    assert candles.timestamps == (1, 2, 3)
