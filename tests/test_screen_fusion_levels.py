"""The funnel, the fusion step, and the levels a fired signal carries."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from support import breakout_closes, make_candles, range_closes, snapshot, trend_closes

from cryptosignal.features import compute_features
from cryptosignal.fusion import fuse
from cryptosignal.legs import score_technical
from cryptosignal.levels import MAX_STOP_ATR, build_levels
from cryptosignal.models import Direction, Driver, LegName, LegScore
from cryptosignal.screen import setup_score, shortlist, stage1_universe

# ---- stage 1 ---------------------------------------------------------------


def test_stage1_drops_thin_markets(settings):
    markets = [
        snapshot("BIG/USDT", "BIG", volume=90_000_000),
        snapshot("THIN/USDT", "THIN", volume=100_000),
    ]
    survivors = stage1_universe(markets, settings)
    assert [m.symbol for m in survivors] == ["BIG/USDT"]


def test_stage1_drops_wide_spreads(settings):
    markets = [
        snapshot("TIGHT/USDT", "TIGHT", spread_bps=3.0),
        snapshot("WIDE/USDT", "WIDE", spread_bps=45.0),
    ]
    assert [m.symbol for m in stage1_universe(markets, settings)] == ["TIGHT/USDT"]


def test_stage1_keeps_markets_with_no_reported_spread(settings):
    """Some venues omit bid/ask; that is a data gap, not an illiquid coin."""
    markets = [snapshot("NOSPREAD/USDT", "NOSPREAD", spread_bps=float("nan"))]
    assert len(stage1_universe(markets, settings)) == 1


def test_stage1_excludes_stablecoins_and_wrapped_tokens(settings):
    markets = [
        snapshot("BTC/USDT", "BTC"),
        snapshot("USDC/USDT", "USDC"),
        snapshot("WBTC/USDT", "WBTC"),
        snapshot("STETH/USDT", "STETH"),
    ]
    assert [m.symbol for m in stage1_universe(markets, settings)] == ["BTC/USDT"]


def test_stage1_excludes_coins_with_an_open_signal(settings):
    markets = [snapshot("BTC/USDT", "BTC"), snapshot("ETH/USDT", "ETH")]
    survivors = stage1_universe(markets, settings, excluded_symbols={"BTC/USDT"})
    assert [m.symbol for m in survivors] == ["ETH/USDT"]


def test_stage1_ignores_other_quote_currencies(settings):
    markets = [snapshot("BTC/EUR", "BTC", quote="EUR"), snapshot("BTC/USDT", "BTC")]
    assert [m.symbol for m in stage1_universe(markets, settings)] == ["BTC/USDT"]


def test_stage1_caps_at_the_universe_size(settings):
    small = replace(settings, universe_size=3)
    markets = [snapshot(f"C{i}/USDT", f"C{i}", volume=10_000_000 * (i + 1)) for i in range(10)]
    survivors = stage1_universe(markets, small)
    assert len(survivors) == 3
    # And keeps the most liquid, not the first three seen.
    assert [m.symbol for m in survivors] == ["C9/USDT", "C8/USDT", "C7/USDT"]


# ---- stage 2 ---------------------------------------------------------------


def test_setup_score_is_higher_for_a_breakout_than_a_quiet_range(settings):
    spike = np.concatenate([np.full(256, 1000.0), np.full(4, 4000.0)])
    live = compute_features(make_candles(breakout_closes(), volumes=spike))
    quiet = compute_features(make_candles(range_closes()))

    live_score = setup_score(snapshot(), live, settings).setup_score
    quiet_score = setup_score(snapshot(), quiet, settings).setup_score
    assert live_score > quiet_score


def test_setup_score_stays_in_range(settings):
    for closes in (trend_closes(), range_closes(), breakout_closes()):
        candidate = setup_score(snapshot(), compute_features(make_candles(closes)), settings)
        assert 0.0 <= candidate.setup_score <= 100.0


def test_setup_score_is_direction_agnostic(settings):
    """A strong downtrend is as much of a setup as a strong uptrend."""
    up = setup_score(snapshot(), compute_features(make_candles(trend_closes(drift=0.006))), settings)
    down = setup_score(snapshot(), compute_features(make_candles(trend_closes(drift=-0.006))), settings)
    assert abs(up.setup_score - down.setup_score) < 40


def test_absent_catalyst_leg_does_not_flatten_every_score(settings):
    """Phase 1 has no news source; its weight is redistributed, not scored zero."""
    candidate = setup_score(snapshot(), compute_features(make_candles(breakout_closes())), settings)
    assert "catalyst" not in candidate.components
    assert candidate.setup_score > 0


def test_shortlist_takes_the_top_n(settings):
    small = replace(settings, shortlist_size=2)
    features = compute_features(make_candles(trend_closes()))
    candidates = []
    for i, score in enumerate([10.0, 90.0, 50.0, 70.0]):
        candidate = setup_score(snapshot(f"C{i}/USDT", f"C{i}"), features, small)
        candidates.append(replace(candidate, setup_score=score))
    picked = shortlist(candidates, small)
    assert [c.setup_score for c in picked] == [90.0, 70.0]


# ---- fusion ----------------------------------------------------------------


def _leg(name: LegName, score: float, available: bool = True) -> LegScore:
    return LegScore(leg=name, score=score, available=available,
                    drivers=(Driver(f"{name.value} driver", score, 1.0, "synthetic"),))


def test_fusion_fires_long_above_the_threshold(settings):
    result = fuse([_leg(LegName.TECHNICAL, 80.0)], settings)
    assert result.direction is Direction.LONG
    assert result.fired


def test_fusion_fires_short_below_the_threshold(settings):
    result = fuse([_leg(LegName.TECHNICAL, -80.0)], settings)
    assert result.direction is Direction.SHORT


def test_fusion_stays_silent_inside_the_band(settings):
    result = fuse([_leg(LegName.TECHNICAL, 30.0)], settings)
    assert result.direction is None
    assert not result.fired
    assert result.confidence == 0.0


def test_single_leg_runs_at_full_strength(settings):
    """Renormalising over reporting legs is what lets phase 1 ship technical-only."""
    result = fuse([_leg(LegName.TECHNICAL, 90.0)], settings)
    assert result.composite == pytest.approx(90.0)


def test_an_unavailable_leg_is_silent_not_neutral(settings):
    reporting_only = fuse([_leg(LegName.TECHNICAL, 90.0),
                           _leg(LegName.FUNDAMENTAL, 0.0, available=False)], settings)
    assert reporting_only.composite == pytest.approx(90.0)
    assert reporting_only.reporting_legs == ("technical",)


def test_three_legs_are_weighted_per_the_spec(settings):
    result = fuse([
        _leg(LegName.TECHNICAL, 100.0),
        _leg(LegName.FUNDAMENTAL, 0.0),
        _leg(LegName.SENTIMENT, 0.0),
    ], settings)
    assert result.composite == pytest.approx(50.0)


def test_disagreeing_legs_still_fire_but_flagged(settings):
    # 100*0.5 + (-40)*0.25 + 100*0.25 = 65, past the long threshold despite
    # the on-chain leg leaning the other way.
    result = fuse([
        _leg(LegName.TECHNICAL, 100.0),
        _leg(LegName.FUNDAMENTAL, -40.0),
        _leg(LegName.SENTIMENT, 100.0),
    ], settings)
    assert result.fired
    assert result.reduced_confidence
    assert "bullish" in result.reduced_confidence_reason and "bearish" in result.reduced_confidence_reason


def test_disagreement_costs_confidence(settings):
    agreeing = fuse([_leg(LegName.TECHNICAL, 100.0), _leg(LegName.FUNDAMENTAL, 100.0),
                     _leg(LegName.SENTIMENT, 100.0)], settings)
    conflicted = fuse([_leg(LegName.TECHNICAL, 100.0), _leg(LegName.FUNDAMENTAL, -60.0),
                       _leg(LegName.SENTIMENT, 100.0)], settings)
    assert conflicted.confidence < agreeing.confidence


def test_a_single_leg_is_flagged_as_thin_evidence(settings):
    result = fuse([_leg(LegName.TECHNICAL, 95.0)], settings)
    assert result.reduced_confidence
    assert "technical leg only" in result.reduced_confidence_reason


def test_confidence_rises_with_the_composite(settings):
    weak = fuse([_leg(LegName.TECHNICAL, 62.0)], settings)
    strong = fuse([_leg(LegName.TECHNICAL, 100.0)], settings)
    assert weak.confidence < strong.confidence
    assert strong.confidence <= settings.confidence_ceiling


def test_confidence_never_exceeds_the_ceiling(settings):
    result = fuse([_leg(LegName.TECHNICAL, 100.0), _leg(LegName.FUNDAMENTAL, 100.0),
                   _leg(LegName.SENTIMENT, 100.0)], settings)
    assert result.confidence <= settings.confidence_ceiling


def test_no_reporting_legs_fires_nothing(settings):
    result = fuse([_leg(LegName.TECHNICAL, 90.0, available=False)], settings)
    assert not result.fired
    assert result.reduced_confidence_reason == "no analysis leg reported"


def test_merged_drivers_take_the_top_three(settings):
    result = fuse([_leg(LegName.TECHNICAL, 90.0), _leg(LegName.FUNDAMENTAL, 70.0),
                   _leg(LegName.SENTIMENT, 80.0)], settings)
    assert len(result.drivers) == 3


# ---- levels ----------------------------------------------------------------


def test_long_levels_are_ordered(settings):
    features = compute_features(make_candles(trend_closes(drift=0.005)))
    levels = build_levels(features, Direction.LONG, settings)
    assert levels.stop < levels.entry_low <= levels.entry_high < levels.target1 < levels.target2


def test_short_levels_are_ordered(settings):
    features = compute_features(make_candles(trend_closes(drift=-0.005)))
    levels = build_levels(features, Direction.SHORT, settings)
    assert levels.target2 < levels.target1 < levels.entry_low <= levels.entry_high < levels.stop


def test_targets_sit_at_the_configured_r_multiples(settings):
    features = compute_features(make_candles(trend_closes()))
    levels = build_levels(features, Direction.LONG, settings)
    assert levels.reward_risk(levels.target1) == pytest.approx(settings.target1_r, rel=0.02)
    assert levels.reward_risk(levels.target2) == pytest.approx(settings.target2_r, rel=0.02)


def test_risk_is_capped_however_far_the_pivot_sits(settings):
    features = compute_features(make_candles(trend_closes()))
    # A support level absurdly far below should not widen the stop indefinitely.
    stretched = replace(features, support=features.close * 0.2)
    levels = build_levels(stretched, Direction.LONG, settings)
    assert (levels.entry_mid - levels.stop) <= MAX_STOP_ATR * features.atr * 1.01


def test_a_pivot_on_the_wrong_side_is_not_used_as_a_stop(settings):
    """'Support' above a long entry is a target, not a stop."""
    features = compute_features(make_candles(trend_closes()))
    inverted = replace(features, support=features.close * 1.5)
    levels = build_levels(inverted, Direction.LONG, settings)
    assert levels.stop < levels.entry_mid


def test_holding_window_respects_the_spec_band(settings):
    for drift in (-0.01, 0.0005, 0.01):
        features = compute_features(make_candles(trend_closes(drift=drift)))
        levels = build_levels(features, Direction.LONG, settings)
        assert settings.min_hold_minutes <= levels.hold_minutes <= settings.max_hold_minutes


def test_levels_refuse_to_build_without_a_usable_atr(settings):
    features = compute_features(make_candles(trend_closes()))
    with pytest.raises(ValueError):
        build_levels(replace(features, atr=float("nan")), Direction.LONG, settings)


def test_levels_round_to_a_readable_precision(settings):
    features = compute_features(make_candles(trend_closes(start=0.00004321)))
    levels = build_levels(features, Direction.LONG, settings)
    for value in (levels.entry_low, levels.stop, levels.target1, levels.target2):
        assert math.isfinite(value)
        assert len(f"{value!r}") < 24


def test_end_to_end_uptrend_produces_a_long_signal(settings):
    """The whole scoring path, on a chart that should obviously be a long."""
    features = compute_features(make_candles(trend_closes(drift=0.006)))
    result = fuse([score_technical(features, settings)], settings)
    assert result.direction is Direction.LONG
    levels = build_levels(features, result.direction, settings)
    assert levels.stop < features.close < levels.target1
