"""The feature pass and the technical leg, on charts with a known shape."""

from __future__ import annotations

import math

import numpy as np
import pytest
from support import breakout_closes, make_candles, range_closes, trend_closes

from cryptosignal.features import MIN_BARS, compute_features
from cryptosignal.legs import score_technical
from cryptosignal.models import LegName


def test_features_need_enough_history():
    assert compute_features(make_candles(trend_closes(MIN_BARS - 1))) is None


def test_features_populate_on_a_full_series():
    features = compute_features(make_candles(trend_closes()))
    assert features is not None
    assert features.bars == 260
    assert features.has_long_trend
    assert math.isfinite(features.atr) and features.atr > 0
    assert math.isfinite(features.rsi)
    assert math.isfinite(features.vwap)


def test_uptrend_stacks_the_emas():
    features = compute_features(make_candles(trend_closes(drift=0.005)))
    assert features.ema9 > features.ema21 > features.ema50 > features.ema200
    assert features.close > features.ema200


def test_downtrend_inverts_the_stack():
    features = compute_features(make_candles(trend_closes(drift=-0.005)))
    assert features.ema9 < features.ema21 < features.ema50 < features.ema200


def test_breakout_is_detected_as_a_resistance_break():
    features = compute_features(make_candles(breakout_closes()))
    assert features.broke_resistance
    assert not features.broke_support


def test_a_quiet_range_breaks_nothing():
    features = compute_features(make_candles(range_closes()))
    assert not features.broke_resistance
    assert not features.broke_support


def test_volume_spike_shows_up_as_relative_volume():
    volumes = np.concatenate([np.full(259, 1000.0), [4000.0]])
    features = compute_features(make_candles(trend_closes(), volumes=volumes))
    assert features.rvol == pytest.approx(4.0, rel=0.02)


def test_atr_expansion_measures_against_its_own_baseline():
    calm = trend_closes(240, drift=0.0005, noise=0.0002)
    violent = calm[-1] * np.cumprod(1.0 + np.array([0.03, -0.028, 0.032, -0.03, 0.035] * 4))
    features = compute_features(make_candles(np.concatenate([calm, violent])))
    assert features.atr_expansion > 1.5


# ---- the leg itself --------------------------------------------------------

def test_uptrend_scores_long(settings):
    leg = score_technical(compute_features(make_candles(trend_closes(drift=0.005))), settings)
    assert leg.leg is LegName.TECHNICAL
    assert leg.available
    assert leg.score > 40


def test_downtrend_scores_short(settings):
    leg = score_technical(compute_features(make_candles(trend_closes(drift=-0.005))), settings)
    assert leg.score < -40


def test_range_scores_near_neutral(settings):
    leg = score_technical(compute_features(make_candles(range_closes())), settings)
    assert abs(leg.score) < settings.long_threshold


def test_score_is_always_in_range(settings):
    for drift in (-0.02, -0.005, 0.0, 0.005, 0.02):
        leg = score_technical(compute_features(make_candles(trend_closes(drift=drift))), settings)
        assert -100.0 <= leg.score <= 100.0


def test_missing_features_make_the_leg_unavailable(settings):
    leg = score_technical(None, settings)
    assert not leg.available
    assert leg.score == 0.0
    assert "history" in leg.note


def test_every_component_reports_a_driver(settings):
    leg = score_technical(compute_features(make_candles(trend_closes())), settings)
    labels = {d.label for d in leg.drivers}
    assert labels == {"Trend", "Momentum", "Volatility", "Volume", "Structure"}


def test_drivers_explain_themselves(settings):
    """A card shows the top three factors; a factor with no text explains nothing."""
    leg = score_technical(compute_features(make_candles(trend_closes())), settings)
    for driver in leg.top_drivers(3):
        assert driver.detail.strip()


def test_top_drivers_rank_by_contribution_not_raw_score(settings):
    leg = score_technical(compute_features(make_candles(trend_closes())), settings)
    ranked = leg.top_drivers(5)
    contributions = [abs(d.contribution) for d in ranked]
    assert contributions == sorted(contributions, reverse=True)


def test_a_silent_component_does_not_drag_the_score_to_neutral(settings):
    """A component with no data abstains; abstaining is not a vote for zero."""
    features = compute_features(make_candles(trend_closes(drift=0.006)))
    full = score_technical(features, settings)

    from dataclasses import replace
    blinded = replace(features, obv_slope_norm=float("nan"), rvol=float("nan"))
    partial = score_technical(blinded, settings)

    assert partial.available
    assert abs(partial.score) > 40
    # Losing the volume vote should not halve a strong directional read.
    assert abs(partial.score - full.score) < 25


def test_too_few_components_makes_the_leg_unavailable(settings):
    from dataclasses import replace

    features = compute_features(make_candles(trend_closes()))
    crippled = replace(
        features,
        ema9=float("nan"), ema21=float("nan"), ema50=float("nan"), ema200=float("nan"),
        rsi=float("nan"), macd_hist=float("nan"), stoch_k=float("nan"),
        bb_position=float("nan"), obv_slope_norm=float("nan"),
    )
    leg = score_technical(crippled, settings)
    assert not leg.available
    assert "components" in leg.note


def test_momentum_peaks_before_the_extremes(settings):
    """A stretched oscillator is a worse entry than a strong one, and scores lower."""
    from dataclasses import replace

    from cryptosignal.legs.technical import _momentum

    base = compute_features(make_candles(trend_closes()))
    strong = _momentum(replace(base, rsi=75.0, stoch_k=80.0), 1.0)
    extreme = _momentum(replace(base, rsi=98.0, stoch_k=99.0), 1.0)
    assert extreme.score < strong.score
    assert "exhausted" in extreme.detail


def test_oscillator_decay_is_symmetric():
    from cryptosignal.legs.technical import RSI_EXHAUSTED_HIGH, RSI_EXHAUSTED_LOW, _oscillator

    high, high_flag = _oscillator(98.0, RSI_EXHAUSTED_LOW, RSI_EXHAUSTED_HIGH)
    low, low_flag = _oscillator(2.0, RSI_EXHAUSTED_LOW, RSI_EXHAUSTED_HIGH)
    assert high_flag and low_flag
    assert high == pytest.approx(-low, rel=0.05)


def test_oscillator_is_neutral_in_the_middle():
    from cryptosignal.legs.technical import _oscillator

    score, exhausted = _oscillator(50.0, 22.0, 78.0)
    assert score == pytest.approx(0.0)
    assert not exhausted
