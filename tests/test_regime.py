"""Regime classification and higher-timeframe confluence.

Both are filters that run before a setup is even considered, and both are
judged here on the property that makes them worth having: they must let a
genuine reversal through while cutting the signals that fight a strong trend
above them. A filter that vetoes every counter-trend signal misses every
reversal, and one that vetoes nothing is decoration.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from support import make_candles, range_closes, trend_closes

from cryptosignal.fusion import fuse
from cryptosignal.models import Direction, Driver, LegName, LegScore
from cryptosignal.regime import (
    HigherTimeframe,
    Regime,
    apply_confluence,
    classify,
    efficiency_ratio,
    higher_timeframe_for,
    higher_timeframe_trend,
)


def leg(score: float) -> LegScore:
    return LegScore(leg=LegName.TECHNICAL, score=score,
                    drivers=(Driver("Trend", score, 1.0, "synthetic"),))


# ---- efficiency ratio ------------------------------------------------------


def test_a_straight_line_is_perfectly_efficient():
    assert efficiency_ratio(np.arange(50, dtype=float), 20) == pytest.approx(1.0)


def test_a_treadmill_is_inefficient():
    """Same start and end, lots of walking."""
    series = np.tile([100.0, 101.0], 30)
    assert efficiency_ratio(series, 20) < 0.1


def test_a_flat_series_is_zero_not_a_divide_by_zero():
    assert efficiency_ratio(np.full(50, 100.0), 20) == 0.0


def test_too_little_history_is_nan():
    import math

    assert math.isnan(efficiency_ratio(np.arange(5, dtype=float), 20))


# ---- classification --------------------------------------------------------


def test_a_clean_trend_classifies_as_trending():
    reading = classify(make_candles(trend_closes(300, drift=0.005, noise=0.0008)))
    assert reading.regime is Regime.TRENDING
    assert reading.efficiency > 0.35


def test_an_oscillation_is_not_trending():
    reading = classify(make_candles(range_closes(300)))
    assert reading.regime is not Regime.TRENDING


def test_a_tight_chop_classifies_as_choppy():
    """Price going nowhere through a lot of small moves."""
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0, 0.05, 300)) * 0.1
    reading = classify(make_candles(closes))
    assert reading.regime in (Regime.CHOPPY, Regime.RANGING)


def test_chop_is_the_regime_marked_untradeable():
    assert not Regime.CHOPPY.is_tradeable
    assert Regime.TRENDING.is_tradeable
    assert Regime.RANGING.is_tradeable


def test_too_little_history_classifies_as_nothing():
    assert classify(make_candles(trend_closes(20))) is None


def test_the_reading_publishes_its_inputs():
    reading = classify(make_candles(trend_closes(300, drift=0.005))).to_dict()
    for key in ("regime", "efficiency", "adx", "detail"):
        assert key in reading


# ---- higher timeframe ------------------------------------------------------


def test_an_uptrend_reads_up():
    higher = higher_timeframe_trend(make_candles(trend_closes(200, drift=0.006), timeframe="4h"))
    assert higher.direction == 1
    assert higher.strength > 0
    assert "4h" in higher.detail


def test_a_downtrend_reads_down():
    higher = higher_timeframe_trend(make_candles(trend_closes(200, drift=-0.006), timeframe="4h"))
    assert higher.direction == -1


def test_a_turning_higher_timeframe_declines_to_pick_a_side():
    """Price below a rising stack is a trend in transition, not a direction."""
    # Eight bars of -1.5% puts price under the slow EMA while the fast EMA is
    # still above it. A bigger drop would just be a downtrend, correctly read.
    rising = trend_closes(200, drift=0.004, seed=2)
    turning = np.concatenate([rising, rising[-1] * np.cumprod(np.full(8, 0.985))])
    higher = higher_timeframe_trend(make_candles(turning, timeframe="4h"))
    assert higher.direction == 0
    assert "turning" in higher.detail


def test_too_little_higher_timeframe_history_is_none():
    assert higher_timeframe_trend(make_candles(trend_closes(30), timeframe="4h")) is None


def test_the_ladder_maps_each_timeframe_up(settings):
    assert higher_timeframe_for("15m", settings) == "4h"
    assert higher_timeframe_for("1h", settings) == "1d"
    # An unlisted timeframe falls back rather than failing.
    assert higher_timeframe_for("7m", settings) == settings.default_higher_timeframe


# ---- confluence ------------------------------------------------------------


def up(strength: float = 0.9) -> HigherTimeframe:
    return HigherTimeframe("4h", 1, strength, "4h trend up")


def down(strength: float = 0.9) -> HigherTimeframe:
    return HigherTimeframe("4h", -1, strength, "4h trend down")


def test_an_aligned_trend_raises_the_score(settings):
    boosted, note = apply_confluence(70.0, up(), settings)
    assert boosted > 70.0
    assert "agrees" in note


def test_a_strongly_opposed_trend_vetoes(settings):
    vetoed, note = apply_confluence(70.0, down(0.9), settings)
    assert vetoed == 0.0
    assert "vetoed" in note


def test_a_weakly_opposed_trend_only_costs_size(settings):
    """Every reversal starts as a counter-trend signal. A hard veto misses them all."""
    cut, note = apply_confluence(70.0, down(0.3), settings)
    assert 0 < cut < 70.0
    assert "opposed" in note


def test_an_undecided_higher_timeframe_changes_nothing(settings):
    unchanged, note = apply_confluence(70.0, HigherTimeframe("4h", 0, 0.0, "turning"), settings)
    assert unchanged == 70.0
    assert note == ""


def test_confluence_is_symmetric_for_shorts(settings):
    """A short into a strong uptrend is the same mistake, mirrored."""
    vetoed, _ = apply_confluence(-70.0, up(0.9), settings)
    assert vetoed == 0.0
    boosted, _ = apply_confluence(-70.0, down(0.9), settings)
    assert boosted < -70.0


def test_no_higher_timeframe_leaves_the_score_alone(settings):
    assert apply_confluence(70.0, None, settings) == (70.0, "")


# ---- through fusion --------------------------------------------------------


def test_fusion_vetoes_a_call_that_fights_the_higher_timeframe(settings):
    result = fuse([leg(90.0)], settings, higher=down(0.9))
    assert not result.fired
    assert "vetoed" in result.suppressed_reason
    assert result.raw_composite == pytest.approx(90.0)     # what it would have been


def test_fusion_keeps_a_call_the_higher_timeframe_supports(settings):
    result = fuse([leg(75.0)], settings, higher=up(0.9))
    assert result.direction is Direction.LONG
    assert result.composite > result.raw_composite


def test_fusion_skips_a_choppy_market(settings):
    reading = classify(make_candles(range_closes(300, amplitude=0.001)))
    forced = replace_regime(reading, Regime.CHOPPY)
    result = fuse([leg(90.0)], settings, regime=forced)
    assert not result.fired
    assert "skipped" in result.suppressed_reason


def test_the_chop_skip_can_be_turned_off(settings):
    reading = replace_regime(classify(make_candles(range_closes(300))), Regime.CHOPPY)
    allowed = replace(settings, skip_choppy_regime=False)
    assert fuse([leg(90.0)], allowed, regime=reading).fired


def test_fusion_reports_the_regime_on_the_call(settings):
    trending = classify(make_candles(trend_closes(300, drift=0.005, noise=0.0008)))
    result = fuse([leg(80.0)], settings, regime=trending)
    assert result.regime == "trending"
    assert result.fired


def test_confluence_can_be_disabled_entirely(settings):
    off = replace(settings, enable_htf_confluence=False)
    result = fuse([leg(90.0)], off, higher=down(0.9))
    assert result.fired                      # the veto never ran


def replace_regime(reading, regime):
    from dataclasses import replace as dc_replace

    return dc_replace(reading, regime=regime)
