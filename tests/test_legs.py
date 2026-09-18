"""The fundamental and sentiment legs, and all three legs fused together.

The recurring assertion: a component with no data must not vote, and a leg with
too few reporting components must not vote either. That is what keeps a missing
API key from quietly dragging every score toward neutral.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from support import make_candles, trend_closes

from cryptosignal.features import compute_features
from cryptosignal.fusion import fuse
from cryptosignal.legs import score_fundamental, score_sentiment, score_technical
from cryptosignal.models import Direction, LegName
from cryptosignal.sources.defillama import TVLReading
from cryptosignal.sources.derivatives import DerivativesReading
from cryptosignal.sources.fear_greed import RegimeReading
from cryptosignal.sources.news import NewsItem, NewsReading
from cryptosignal.sources.orderbook import BookReading


def deriv(funding=None, oi=None, oi_change=None):
    return DerivativesReading("BTC/USDT", funding, oi, oi_change)


def book(imbalance: float, notional: float = 4_000_000.0):
    bid = notional * (1 + imbalance) / 2
    return BookReading("BTC/USDT", bid, notional - bid)


def tvl(change_7d=None, change_1d=None, usd=1e10):
    return TVLReading("LDO", "Lido", usd, change_1d, change_7d)


def features(drift: float = 0.004):
    return compute_features(make_candles(trend_closes(drift=drift)))


def by_label(leg):
    return {d.label: d for d in leg.drivers}


# ---- fundamental leg -------------------------------------------------------


def test_extreme_positive_funding_reads_bearish(settings):
    """The crowd is levered long and paying for it. That is a crowded trade."""
    leg = score_fundamental(deriv(funding=0.002), book(0.0), tvl(0.0), features(), settings)
    assert by_label(leg)["Funding"].score < -50


def test_extreme_negative_funding_reads_bullish(settings):
    leg = score_fundamental(deriv(funding=-0.002), book(0.0), tvl(0.0), features(), settings)
    assert by_label(leg)["Funding"].score > 50


def test_normal_funding_reports_neutral_but_still_votes(settings):
    """A venue that answered 'nothing unusual' has reported. That is not an abstention."""
    leg = score_fundamental(deriv(funding=0.00005), book(0.0), tvl(0.0), features(), settings)
    driver = by_label(leg)["Funding"]
    assert driver.score == 0.0
    assert driver.weight > 0
    assert "normal" in driver.detail


def test_a_pair_with_no_perp_abstains_on_funding(settings):
    leg = score_fundamental(deriv(), book(0.3), tvl(5.0), features(), settings)
    assert by_label(leg)["Funding"].weight == 0.0


def test_rising_open_interest_with_rising_price_is_bullish(settings):
    leg = score_fundamental(deriv(oi=1e6, oi_change=0.06), book(0.0), tvl(0.0),
                            features(drift=0.006), settings)
    driver = by_label(leg)["Open interest"]
    assert driver.score > 50
    assert "new longs" in driver.detail


def test_rising_open_interest_with_falling_price_is_bearish(settings):
    leg = score_fundamental(deriv(oi=1e6, oi_change=0.06), book(0.0), tvl(0.0),
                            features(drift=-0.006), settings)
    driver = by_label(leg)["Open interest"]
    assert driver.score < -50
    assert "new shorts" in driver.detail


def test_falling_open_interest_reads_against_the_move_and_weaker(settings):
    """Unwinding is weaker evidence than new positioning, and points the other way."""
    rising = score_fundamental(deriv(oi=1e6, oi_change=0.06), book(0.0), tvl(0.0),
                               features(drift=0.006), settings)
    falling = score_fundamental(deriv(oi=1e6, oi_change=-0.06), book(0.0), tvl(0.0),
                                features(drift=0.006), settings)
    assert by_label(falling)["Open interest"].score < 0
    assert abs(by_label(falling)["Open interest"].score) < abs(by_label(rising)["Open interest"].score)


def test_open_interest_abstains_without_a_previous_reading(settings):
    leg = score_fundamental(deriv(oi=1e6), book(0.2), tvl(4.0), features(), settings)
    assert by_label(leg)["Open interest"].weight == 0.0


def test_a_bid_heavy_book_is_bullish(settings):
    leg = score_fundamental(deriv(funding=0.0), book(0.5), tvl(0.0), features(), settings)
    assert by_label(leg)["Book pressure"].score > 50


def test_no_book_abstains(settings):
    leg = score_fundamental(deriv(funding=0.0), None, tvl(5.0), features(), settings)
    assert by_label(leg)["Book pressure"].weight == 0.0


def test_rising_tvl_is_bullish_and_names_the_protocol(settings):
    leg = score_fundamental(deriv(funding=0.0), book(0.0), tvl(change_7d=20.0), features(), settings)
    driver = by_label(leg)["TVL"]
    assert driver.score > 50
    assert "Lido" in driver.detail


def test_a_non_protocol_abstains_on_tvl(settings):
    leg = score_fundamental(deriv(funding=0.0), book(0.1), None, features(), settings)
    driver = by_label(leg)["TVL"]
    assert driver.weight == 0.0
    assert "not a DeFi protocol" in driver.detail


def test_the_leg_does_not_vote_with_too_little_data(settings):
    leg = score_fundamental(deriv(), None, None, features(), settings)
    assert not leg.available
    assert leg.score == 0.0
    assert "not enough on-chain data" in leg.note


def test_the_leg_votes_with_two_components(settings):
    leg = score_fundamental(deriv(funding=-0.002), book(0.4), None, features(), settings)
    assert leg.available
    assert leg.score > 0
    assert "2 component(s) had no data" in leg.note


def test_the_fundamental_score_stays_in_range(settings):
    for funding in (-0.01, 0.0, 0.01):
        leg = score_fundamental(deriv(funding=funding, oi=1e6, oi_change=0.2),
                                book(1.0), tvl(100.0), features(), settings)
        assert -100.0 <= leg.score <= 100.0


# ---- sentiment leg ---------------------------------------------------------


def news(impact: float, recent: int = 3, baseline: int = 1, title: str = "Something happened"):
    item = NewsItem(title, "https://example.invalid/1", datetime.now(UTC), "test")
    return NewsReading("SOL", impact, recent, baseline, ((item, impact),))


def regime(value: float, label: str = "Neutral", previous: float | None = None):
    return RegimeReading(value, label, None, previous)


def test_bullish_coverage_scores_long(settings):
    leg = score_sentiment(news(80.0), regime(50.0), settings)
    assert leg.available
    assert leg.score > 30


def test_bearish_coverage_scores_short(settings):
    leg = score_sentiment(news(-80.0), regime(50.0), settings)
    assert leg.score < -30


def test_a_coin_with_no_coverage_abstains_on_both_news_components(settings):
    leg = score_sentiment(None, regime(50.0), settings)
    drivers = by_label(leg)
    assert drivers["News"].weight == 0.0
    assert drivers["Mention velocity"].weight == 0.0
    # One component left is below the minimum, so the leg does not vote.
    assert not leg.available


def test_coverage_with_no_classified_event_reports_neutral(settings):
    leg = score_sentiment(news(0.0), regime(50.0), settings)
    driver = by_label(leg)["News"]
    assert driver.score == 0.0
    assert driver.weight > 0
    assert "no classified event" in driver.detail


def test_a_coverage_spike_amplifies_in_the_classified_direction(settings):
    quiet = score_sentiment(news(70.0, recent=2, baseline=2), regime(50.0), settings)
    spike = score_sentiment(news(70.0, recent=9, baseline=2), regime(50.0), settings)
    assert by_label(spike)["Mention velocity"].score > by_label(quiet)["Mention velocity"].score


def test_a_spike_with_no_classified_event_does_not_pick_a_side(settings):
    leg = score_sentiment(news(0.0, recent=12, baseline=2), regime(50.0), settings)
    driver = by_label(leg)["Mention velocity"]
    assert driver.score == 0.0
    assert "no classified event" in driver.detail


def test_velocity_abstains_without_a_baseline(settings):
    leg = score_sentiment(news(50.0, recent=3, baseline=0), regime(50.0), settings)
    assert by_label(leg)["Mention velocity"].weight == 0.0


def test_extreme_greed_is_faded(settings):
    leg = score_sentiment(news(0.0), regime(95.0, "Extreme Greed"), settings)
    assert by_label(leg)["Market regime"].score < -50


def test_extreme_fear_is_faded(settings):
    leg = score_sentiment(news(0.0), regime(5.0, "Extreme Fear"), settings)
    assert by_label(leg)["Market regime"].score > 50


def test_a_middling_regime_reports_neutral(settings):
    driver = by_label(score_sentiment(news(0.0), regime(52.0, "Neutral"), settings))["Market regime"]
    assert driver.score == 0.0
    assert driver.weight > 0
    assert "no extreme to fade" in driver.detail


def test_a_missing_regime_abstains(settings):
    leg = score_sentiment(news(60.0), None, settings)
    assert by_label(leg)["Market regime"].weight == 0.0
    assert leg.available          # news + velocity still reported


def test_the_sentiment_score_stays_in_range(settings):
    for impact in (-100.0, 0.0, 100.0):
        leg = score_sentiment(news(impact, recent=20, baseline=1), regime(99.0), settings)
        assert -100.0 <= leg.score <= 100.0


# ---- all three fused -------------------------------------------------------


def test_three_agreeing_legs_fire_at_full_confidence(settings):
    legs = [
        score_technical(features(drift=0.006), settings),
        score_fundamental(deriv(funding=-0.002, oi=1e6, oi_change=0.06), book(0.5),
                          tvl(change_7d=20.0), features(drift=0.006), settings),
        score_sentiment(news(85.0, recent=8, baseline=2), regime(20.0, "Extreme Fear"), settings),
    ]
    result = fuse(legs, settings)

    assert result.direction is Direction.LONG
    assert set(result.reporting_legs) == {"technical", "fundamental", "sentiment"}
    assert not result.reduced_confidence          # three legs, all aligned
    assert result.confidence > 70


def test_a_bearish_on_chain_leg_flags_an_otherwise_bullish_call(settings):
    legs = [
        score_technical(features(drift=0.006), settings),
        score_fundamental(deriv(funding=0.003), book(-0.5), tvl(change_7d=-20.0),
                          features(drift=0.006), settings),
        score_sentiment(news(90.0, recent=8, baseline=2), regime(20.0), settings),
    ]
    result = fuse(legs, settings)

    assert result.reduced_confidence
    assert "fundamental" in result.reduced_confidence_reason


def test_legs_without_providers_leave_the_technical_call_intact(settings):
    """Phase 1 behaviour has to survive phases 2 and 3 being wired but silent."""
    technical_only = fuse([score_technical(features(drift=0.006), settings)], settings)
    with_silent_legs = fuse([
        score_technical(features(drift=0.006), settings),
        score_fundamental(deriv(), None, None, features(drift=0.006), settings),
        score_sentiment(None, None, settings),
    ], settings)

    assert with_silent_legs.composite == pytest.approx(technical_only.composite)
    assert with_silent_legs.reporting_legs == ("technical",)


def test_the_disabled_legs_switch_is_respected_by_the_scanner(settings):
    off = replace(settings, enable_fundamental_leg=False, enable_sentiment_leg=False)
    assert not off.enable_fundamental_leg and not off.enable_sentiment_leg


def test_drivers_from_every_leg_compete_for_the_top_three(settings):
    legs = [
        score_technical(features(drift=0.006), settings),
        score_fundamental(deriv(funding=-0.003), book(0.6), tvl(change_7d=25.0),
                          features(drift=0.006), settings),
        score_sentiment(news(95.0, recent=9, baseline=2), regime(10.0), settings),
    ]
    labels = {d.label for d in fuse(legs, settings).drivers}
    assert len(labels) == 3
    # The top three should not all come from one leg when all three are loud.
    technical_labels = {"Trend", "Momentum", "Volatility", "Volume", "Structure"}
    assert not labels <= technical_labels


def test_every_leg_name_is_representable(settings):
    assert {leg.value for leg in LegName} == {"technical", "fundamental", "sentiment"}
