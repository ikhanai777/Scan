"""Run the whole engine over real recorded market data, when any exists.

These tests are skipped until someone runs `cryptosignal record` on a machine
with a route to an exchange. That is the point: they are the part of the suite
that is not synthetic, and they cannot be faked into existence here.

What they assert is different from every other test file. Nobody knows what
BTC *should* have scored last Tuesday, so asserting an outcome would be
inventing one. Instead they assert invariants that must hold on any real
market: scores stay in range, no NaN escapes into a signal, levels stay
ordered, and the engine survives shapes a synthetic series never produces --
halted bars, repeated closes, volume spikes, gaps.
"""

from __future__ import annotations

import math
from dataclasses import fields

import pytest

from cryptosignal.backtest import backtest_candles
from cryptosignal.config import Settings
from cryptosignal.features import MIN_BARS, compute_features
from cryptosignal.fixtures import load_fixtures
from cryptosignal.fusion import fuse
from cryptosignal.legs import score_technical
from cryptosignal.levels import build_levels
from cryptosignal.models import Direction, MarketSnapshot
from cryptosignal.screen import setup_score

FIXTURES = load_fixtures()

pytestmark = pytest.mark.skipif(
    not FIXTURES,
    reason="no recorded market data -- run `cryptosignal record` on a networked machine",
)


def ids(fixtures):
    return [f"{f.exchange}:{f.symbol}" for f in fixtures]


@pytest.fixture(params=FIXTURES, ids=ids(FIXTURES))
def fixture(request):
    return request.param


def snapshot_for(fixture, price):
    return MarketSnapshot(
        symbol=fixture.symbol, base=fixture.symbol.split("/")[0],
        quote=fixture.symbol.partition("/")[2], last=price,
        quote_volume_24h=float("nan"), spread_bps=float("nan"), change_24h_pct=float("nan"),
    )


# ---- the recording itself --------------------------------------------------


def test_the_recording_has_enough_history(fixture):
    assert len(fixture.candles) >= MIN_BARS


def test_the_bars_are_ordered_and_sane(fixture):
    candles = fixture.candles
    for i in range(len(candles)):
        assert candles.high[i] >= candles.low[i]
        assert candles.high[i] >= candles.close[i] >= candles.low[i]
        assert candles.volume[i] >= 0
    assert list(candles.timestamps) == sorted(candles.timestamps)


def test_the_recording_is_not_ancient(fixture):
    """A fixture older than a quarter is testing a market that no longer exists."""
    if fixture.age_days > 120:
        pytest.skip(f"fixture is {fixture.age_days:.0f} days old -- re-record it")


# ---- the feature pass ------------------------------------------------------


def test_features_compute_on_real_bars(fixture):
    assert compute_features(fixture.candles) is not None


def test_no_indicator_returns_an_infinity(fixture):
    """NaN is an honest 'not warmed up'. Infinity is always a bug."""
    features = compute_features(fixture.candles)
    for field in fields(features):
        value = getattr(features, field.name)
        if isinstance(value, float):
            assert not math.isinf(value), f"{field.name} is infinite on {fixture.symbol}"


def test_core_readings_warm_up_on_real_history(fixture):
    features = compute_features(fixture.candles)
    for name in ("atr", "rsi", "close"):
        assert math.isfinite(getattr(features, name)), f"{name} never warmed up"
    assert features.atr > 0


# ---- scoring ---------------------------------------------------------------


def test_the_technical_leg_scores_in_range(fixture):
    leg = score_technical(compute_features(fixture.candles), Settings())
    assert -100.0 <= leg.score <= 100.0
    assert math.isfinite(leg.score)


def test_every_reporting_component_explains_itself(fixture):
    leg = score_technical(compute_features(fixture.candles), Settings())
    for driver in leg.drivers:
        if driver.weight > 0:
            assert driver.detail.strip(), f"{driver.label} voted without a reason"
        assert -100.0 <= driver.score <= 100.0


def test_the_setup_score_stays_in_range(fixture):
    features = compute_features(fixture.candles)
    candidate = setup_score(snapshot_for(fixture, features.close), features, Settings())
    assert 0.0 <= candidate.setup_score <= 100.0


def test_levels_are_ordered_whichever_way_it_fires(fixture):
    settings = Settings()
    features = compute_features(fixture.candles)
    for direction in (Direction.LONG, Direction.SHORT):
        levels = build_levels(features, direction, settings)
        assert levels.risk_per_unit > 0
        if direction is Direction.LONG:
            assert levels.stop < levels.entry_low <= levels.entry_high < levels.target1 < levels.target2
        else:
            assert levels.target2 < levels.target1 < levels.entry_low <= levels.entry_high < levels.stop
        assert settings.min_hold_minutes <= levels.hold_minutes <= settings.max_hold_minutes


def test_fusion_produces_a_coherent_call(fixture):
    settings = Settings()
    result = fuse([score_technical(compute_features(fixture.candles), settings)], settings)
    assert -100.0 <= result.composite <= 100.0
    if result.fired:
        assert settings.confidence_floor * 0.7 <= result.confidence <= settings.confidence_ceiling
        assert result.drivers


# ---- the replay ------------------------------------------------------------


def test_the_backtest_runs_over_real_history(fixture):
    result = backtest_candles(fixture.candles, Settings())
    assert result.bars == len(fixture.candles)
    for trade in result.trades:
        assert math.isfinite(trade.realized_r)
        assert trade.exit_index >= trade.entry_index
        assert trade.peak_r >= trade.trough_r


def test_a_stop_on_real_bars_still_costs_exactly_one_r(fixture):
    result = backtest_candles(fixture.candles, Settings())
    for trade in result.trades:
        if trade.outcome == "stop":
            assert trade.realized_r == pytest.approx(-1.0)


def test_replayed_trades_never_overlap(fixture):
    trades = backtest_candles(fixture.candles, Settings()).trades
    for earlier, later in zip(trades, trades[1:], strict=False):
        assert later.entry_index > earlier.exit_index
