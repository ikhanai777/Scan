"""Persistence and outcome grading -- the basis of the published track record."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cryptosignal.models import (
    Direction,
    Driver,
    Levels,
    ScanReport,
    Signal,
    SignalStatus,
    expiry_for,
    utcnow,
)
from cryptosignal.store import Store
from cryptosignal.tracker import update_open_signals


@pytest.fixture
def store() -> Store:
    store = Store(":memory:")
    yield store
    store.close()


def make_signal(symbol: str = "BTC/USDT", direction: Direction = Direction.LONG,
                entry: float = 100.0, hold_minutes: int = 120, **overrides) -> Signal:
    """A long risking 5 to make 7.5 and 12.5, or its mirror image."""
    sign = direction.sign
    levels = Levels(
        entry_low=entry - 0.5, entry_high=entry + 0.5,
        stop=entry - sign * 5.0,
        target1=entry + sign * 7.5,
        target2=entry + sign * 12.5,
        hold_minutes=hold_minutes,
    )
    created_at = overrides.pop("created_at", utcnow())
    return Signal(
        id=overrides.pop("id", f"sig-{symbol}-{direction.value}"),
        symbol=symbol, base=symbol.split("/")[0], direction=direction,
        composite_score=sign * 72.0, confidence=70.0, levels=levels,
        drivers=(Driver("Trend", sign * 80.0, 0.3, "EMA stack aligned"),),
        leg_scores={"technical": sign * 72.0},
        reduced_confidence=False, reduced_confidence_reason="",
        reference_price=entry, setup_score=64.0,
        created_at=created_at, expires_at=expiry_for(created_at, hold_minutes),
        **overrides,
    )


# ---- store -----------------------------------------------------------------


def test_round_trip_preserves_every_field(store):
    original = make_signal()
    store.insert_signal(original)
    loaded = store.get_signal(original.id)

    assert loaded.symbol == original.symbol
    assert loaded.direction is original.direction
    assert loaded.levels == original.levels
    assert loaded.drivers[0].label == "Trend"
    assert loaded.leg_scores == {"technical": 72.0}
    assert loaded.created_at == original.created_at
    assert loaded.status is SignalStatus.OPEN


def test_inserting_a_signal_writes_a_fired_event(store):
    signal = make_signal()
    store.insert_signal(signal)
    events = store.events_for(signal.id)
    assert [e["kind"] for e in events] == ["fired"]


def test_open_symbols_reflects_only_live_signals(store):
    live = make_signal("BTC/USDT")
    done = make_signal("ETH/USDT", id="closed-one")
    store.insert_signal(live)
    store.insert_signal(done)
    done.status = SignalStatus.CLOSED_STOP
    done.realized_r = -1.0
    store.update_outcome(done)

    assert store.open_symbols() == {"BTC/USDT"}


def test_filters_narrow_the_feed(store):
    store.insert_signal(make_signal("BTC/USDT", Direction.LONG, id="a"))
    store.insert_signal(make_signal("ETH/USDT", Direction.SHORT, id="b"))

    assert len(store.recent_signals(direction="long")) == 1
    assert len(store.recent_signals(symbol="ETH/USDT")) == 1
    assert len(store.recent_signals(min_confidence=90.0)) == 0
    assert len(store.recent_signals(status="open")) == 2


def test_performance_starts_empty_and_says_so(store):
    performance = store.performance()
    assert performance["closed"] == 0
    assert performance["hit_rate"] is None
    assert "honest" in performance["note"]


def test_performance_counts_wins_losses_and_drawdown(store):
    outcomes = [2.5, -1.0, -1.0, 1.5]
    for i, realized in enumerate(outcomes):
        signal = make_signal(f"C{i}/USDT", id=f"s{i}")
        store.insert_signal(signal)
        signal.status = SignalStatus.CLOSED_TARGET if realized > 0 else SignalStatus.CLOSED_STOP
        signal.realized_r = realized
        signal.closed_at = signal.created_at + timedelta(minutes=30)
        store.update_outcome(signal)

    performance = store.performance()
    assert performance["closed"] == 4
    assert performance["wins"] == 2 and performance["losses"] == 2
    assert performance["hit_rate"] == pytest.approx(50.0)
    assert performance["total_r"] == pytest.approx(2.0)
    # Equity runs 2.5 -> 1.5 -> 0.5: the worst peak-to-trough is 2.0R.
    assert performance["max_drawdown_r"] == pytest.approx(2.0)
    assert performance["avg_hold_minutes"] == pytest.approx(30.0)


def test_performance_splits_by_direction(store):
    long_signal = make_signal("BTC/USDT", Direction.LONG, id="l")
    short_signal = make_signal("ETH/USDT", Direction.SHORT, id="s")
    for signal, realized in ((long_signal, 1.5), (short_signal, -1.0)):
        store.insert_signal(signal)
        signal.realized_r = realized
        signal.status = SignalStatus.CLOSED_TARGET
        signal.closed_at = utcnow()
        store.update_outcome(signal)

    by_direction = store.performance()["by_direction"]
    assert by_direction["long"]["hit_rate"] == 100.0
    assert by_direction["short"]["hit_rate"] == 0.0


def test_cycle_reports_round_trip(store):
    started = utcnow()
    report = ScanReport(
        started_at=started, finished_at=started + timedelta(seconds=3),
        markets_seen=400, passed_stage1=20, shortlisted=5, analysed=5,
        signals_fired=1, fetch_failures=0, fetch_attempts=21,
    )
    store.record_cycle(report)
    loaded = store.last_cycle()
    assert loaded["markets_seen"] == 400
    assert loaded["duration_seconds"] == pytest.approx(3.0)
    assert loaded["shortlist"] == []


# ---- tracker ---------------------------------------------------------------


def test_target_one_promotes_but_does_not_close(store):
    signal = make_signal()
    store.insert_signal(signal)
    updates = update_open_signals(store, {"BTC/USDT": 108.0})

    assert [u.kind for u in updates] == ["target1"]
    reloaded = store.get_signal(signal.id)
    assert reloaded.status is SignalStatus.TARGET1
    assert reloaded.is_open
    assert reloaded.target1_hit_at is not None


def test_target_two_closes_at_the_configured_multiple(store):
    signal = make_signal()
    store.insert_signal(signal)
    updates = update_open_signals(store, {"BTC/USDT": 113.0})

    assert [u.kind for u in updates] == ["target"]
    reloaded = store.get_signal(signal.id)
    assert reloaded.status is SignalStatus.CLOSED_TARGET
    assert reloaded.realized_r == pytest.approx(2.5, rel=0.01)


def test_a_stop_closes_at_minus_one_r(store):
    signal = make_signal()
    store.insert_signal(signal)
    updates = update_open_signals(store, {"BTC/USDT": 94.0})

    assert [u.kind for u in updates] == ["stop"]
    reloaded = store.get_signal(signal.id)
    assert reloaded.status is SignalStatus.CLOSED_STOP
    assert reloaded.realized_r == pytest.approx(-1.0)


def test_the_stop_wins_a_tie_with_the_target(store):
    """Both levels inside one bar: assume the bad one filled first."""
    signal = make_signal()
    store.insert_signal(signal)
    # A price below the stop is also, technically, not above the target -- so
    # construct the ambiguity directly: a short whose stop and target are both
    # cleared by one print is graded as the stop.
    short = make_signal("ETH/USDT", Direction.SHORT, id="ambiguous")
    store.insert_signal(short)
    updates = update_open_signals(store, {"ETH/USDT": 200.0})   # far above the short's stop
    assert [u.kind for u in updates] == ["stop"]


def test_expiry_marks_to_market_not_to_the_best_price(store):
    created = utcnow() - timedelta(hours=3)
    signal = make_signal(created_at=created, hold_minutes=60)
    store.insert_signal(signal)

    # It ran to +1R at some point, then gave it back before the window closed.
    update_open_signals(store, {"BTC/USDT": 105.0}, now=created + timedelta(minutes=30))
    updates = update_open_signals(store, {"BTC/USDT": 101.0})

    assert [u.kind for u in updates] == ["expired"]
    reloaded = store.get_signal(signal.id)
    assert reloaded.status is SignalStatus.CLOSED_EXPIRED
    assert reloaded.realized_r == pytest.approx(0.2, abs=0.01)
    assert reloaded.peak_r == pytest.approx(1.0, abs=0.01)
    # The R lives in its own field; the reason must not repeat it.
    assert reloaded.close_reason == "holding window expired"


def test_a_short_is_graded_in_its_own_direction(store):
    signal = make_signal("ETH/USDT", Direction.SHORT)
    store.insert_signal(signal)
    updates = update_open_signals(store, {"ETH/USDT": 87.0})

    assert [u.kind for u in updates] == ["target"]
    assert store.get_signal(signal.id).realized_r == pytest.approx(2.5, rel=0.01)


def test_a_missing_price_leaves_the_signal_open(store):
    """A data gap must not close a position by accident."""
    signal = make_signal()
    store.insert_signal(signal)
    assert update_open_signals(store, {}) == []
    assert store.get_signal(signal.id).is_open


def test_peak_and_trough_track_the_excursion(store):
    signal = make_signal()
    store.insert_signal(signal)
    update_open_signals(store, {"BTC/USDT": 104.0})
    update_open_signals(store, {"BTC/USDT": 97.0})

    reloaded = store.get_signal(signal.id)
    assert reloaded.peak_r == pytest.approx(0.8, abs=0.01)
    assert reloaded.trough_r == pytest.approx(-0.6, abs=0.01)


def test_resolution_writes_an_event(store):
    signal = make_signal()
    store.insert_signal(signal)
    update_open_signals(store, {"BTC/USDT": 94.0})
    assert [e["kind"] for e in store.events_for(signal.id)] == ["fired", "stop"]


def test_a_closed_signal_is_not_regraded(store):
    signal = make_signal()
    store.insert_signal(signal)
    update_open_signals(store, {"BTC/USDT": 94.0})
    first = store.get_signal(signal.id).realized_r

    update_open_signals(store, {"BTC/USDT": 130.0})
    assert store.get_signal(signal.id).realized_r == first
