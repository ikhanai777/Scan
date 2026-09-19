"""Correlation control, walk-forward, and whether a result means anything.

These are the three checks that stand between a backtest number and a decision.
The tests are written around the failure they exist to prevent: a book that
looks diversified and is not, a parameter set fitted to one window, and a hit
rate computed from twelve trades.
"""

from __future__ import annotations

import numpy as np
import pytest
from support import make_candles, trend_closes

from cryptosignal.correlation import (
    HIGH_CORRELATION,
    PortfolioHeat,
    correlation,
    returns_of,
)
from cryptosignal.models import Direction
from cryptosignal.validation import significance, walk_forward


def series(seed: int, drift: float = 0.002, symbol: str = "A/USDT"):
    return make_candles(trend_closes(300, drift=drift, seed=seed), symbol=symbol)


def clone_with_noise(base, noise: float, seed: int = 99):
    """A coin that moves with `base` plus its own wobble -- i.e. a real alt."""
    rng = np.random.default_rng(seed)
    closes = np.asarray(base.close) * (1 + rng.normal(0, noise, len(base.close)))
    return make_candles(closes, symbol="B/USDT")


# ---- correlation ------------------------------------------------------------


def test_returns_not_levels_are_correlated():
    """Two coins drifting up have correlated levels whatever they do daily."""
    returns = returns_of(series(1))
    assert returns.size == 299
    assert abs(float(np.mean(returns))) < 0.05


def test_a_coin_is_perfectly_correlated_with_itself():
    base = series(1)
    assert correlation(base, base) == pytest.approx(1.0)


def test_a_near_clone_reads_highly_correlated():
    base = series(1)
    assert correlation(base, clone_with_noise(base, 0.0005)) > 0.7


def test_independent_series_read_low():
    assert abs(correlation(series(1), series(2, symbol="B/USDT"))) < 0.4


def test_too_little_overlap_returns_none_not_zero():
    """'Could not measure' and 'uncorrelated' lead to opposite decisions."""
    short = make_candles(trend_closes(20), symbol="B/USDT")
    assert correlation(series(1), short) is None


def test_a_flat_series_returns_none():
    flat = make_candles(np.full(300, 100.0), symbol="B/USDT")
    assert correlation(series(1), flat) is None


# ---- portfolio heat ---------------------------------------------------------


def test_the_first_position_is_always_allowed():
    heat = PortfolioHeat()
    decision = heat.check("A/USDT", Direction.LONG, 0.5, series(1),
                          budget=1.5, max_correlation=0.8)
    assert decision
    assert "first position" in decision.reason


def test_a_near_duplicate_long_is_refused():
    """The whole point: the same bet twice is not diversification."""
    base = series(1)
    heat = PortfolioHeat()
    heat.add("A/USDT", Direction.LONG, 0.5, base)

    decision = heat.check("B/USDT", Direction.LONG, 0.5, clone_with_noise(base, 0.0005),
                          budget=5.0, max_correlation=0.8)
    assert not decision
    assert "same trade" in decision.reason


def test_an_uncorrelated_long_is_allowed():
    heat = PortfolioHeat()
    heat.add("A/USDT", Direction.LONG, 0.5, series(1))
    decision = heat.check("B/USDT", Direction.LONG, 0.5, series(2, symbol="B/USDT"),
                          budget=1.5, max_correlation=0.8)
    assert decision


def test_an_opposite_side_position_in_a_correlated_coin_nets_down():
    """A long and a short in correlated coins partly hedge, and must not be summed."""
    base = series(1)
    twin = clone_with_noise(base, 0.0005)
    heat = PortfolioHeat()
    heat.add("A/USDT", Direction.LONG, 0.5, base)

    same_side, _, _ = heat.effective_exposure("B/USDT", Direction.LONG, 0.5, twin)
    other_side, _, _ = heat.effective_exposure("B/USDT", Direction.SHORT, 0.5, twin)
    assert other_side < same_side


def test_the_exposure_budget_is_enforced():
    """Correlated positions accumulate; the budget is what stops the pile."""
    # Four positions in the same price series: correlation 1.0 by construction,
    # so effective exposure is the full 2.5 rather than a diversified 0.6.
    base = series(1)
    heat = PortfolioHeat()
    for i in range(4):
        heat.add(f"C{i}/USDT", Direction.LONG, 0.5, base)

    # max_correlation is set out of the way so the *budget* is what refuses.
    decision = heat.check("NEW/USDT", Direction.LONG, 0.5, base,
                          budget=1.0, max_correlation=1.01)
    assert not decision
    assert "budget" in decision.reason


def test_uncorrelated_positions_barely_add_to_exposure():
    """The mirror of the test above: genuine diversification costs little heat."""
    heat = PortfolioHeat()
    for i in range(4):
        heat.add(f"C{i}/USDT", Direction.LONG, 0.5, series(i + 10, symbol=f"C{i}/USDT"))

    exposure, _, _ = heat.effective_exposure("NEW/USDT", Direction.LONG, 0.5,
                                             series(3, symbol="NEW/USDT"))
    assert exposure < 4 * 0.5          # far below the gross sum


def test_an_unmeasurable_correlation_is_assumed_high():
    """The conservative read is the right default when we cannot tell."""
    heat = PortfolioHeat()
    heat.add("A/USDT", Direction.LONG, 0.5, make_candles(trend_closes(20), symbol="A/USDT"))
    exposure, worst, _ = heat.effective_exposure(
        "B/USDT", Direction.LONG, 0.5, series(2, symbol="B/USDT"))
    assert worst == pytest.approx(HIGH_CORRELATION)
    assert exposure > 0.5


def test_removing_a_position_frees_its_exposure():
    heat = PortfolioHeat()
    base = series(1)
    heat.add("A/USDT", Direction.LONG, 0.5, base)
    assert not heat.check("B/USDT", Direction.LONG, 0.5, clone_with_noise(base, 0.0005),
                          budget=5.0, max_correlation=0.8)
    heat.remove("A/USDT")
    assert heat.check("B/USDT", Direction.LONG, 0.5, clone_with_noise(base, 0.0005),
                      budget=5.0, max_correlation=0.8)


def test_the_snapshot_counts_effective_rather_than_actual_positions():
    """Four copies of one trade should not read as four bets."""
    base = series(1)
    heat = PortfolioHeat()
    for i in range(4):
        heat.add(f"C{i}/USDT", Direction.LONG, 0.5, clone_with_noise(base, 0.0005, seed=i))

    snapshot = heat.snapshot()
    assert snapshot["open"] == 4
    assert snapshot["effective_positions"] < 2.0
    assert snapshot["most_correlated"]


def test_independent_positions_count_as_themselves():
    heat = PortfolioHeat()
    for i in range(3):
        heat.add(f"C{i}/USDT", Direction.LONG, 0.5, series(i + 20, symbol=f"C{i}/USDT"))
    assert heat.snapshot()["effective_positions"] > 2.0


# ---- significance -----------------------------------------------------------


def test_a_real_edge_is_called_significant():
    """40 wins at +1.5R against 40 losses at -1R is not noise."""
    sample = [1.5] * 40 + [-1.0] * 40
    stats = significance(sample, iterations=800)
    assert stats.expectancy == pytest.approx(0.25)
    assert stats.low > 0
    assert stats.distinguishable_from_noise
    assert "positive" in stats.verdict()


def test_coin_flips_are_not_called_significant():
    rng = np.random.default_rng(4)
    stats = significance(list(rng.normal(0, 1.0, 200)), iterations=800)
    assert not stats.distinguishable_from_noise
    assert "noise" in stats.verdict()


def test_a_small_sample_says_so_rather_than_guessing():
    stats = significance([2.5, -1.0, 2.5, -1.0, 2.5], iterations=500)
    assert stats.trades == 5
    assert "too few" in stats.verdict()


def test_the_confidence_interval_brackets_the_estimate():
    stats = significance([1.5] * 30 + [-1.0] * 30, iterations=800)
    assert stats.low < stats.expectancy < stats.high


def test_more_trades_narrow_the_interval():
    small = significance([1.5, -1.0] * 15, iterations=800)
    large = significance([1.5, -1.0] * 150, iterations=800)
    assert (large.high - large.low) < (small.high - small.low)


def test_monte_carlo_drawdown_exceeds_the_realised_one():
    """The drawdown you saw was one draw. Sizing against it alone is optimistic."""
    sample = [1.5] * 30 + [-1.0] * 30
    stats = significance(sample, iterations=1000)
    assert stats.worst_max_drawdown >= stats.median_max_drawdown > 0


def test_trades_needed_says_how_far_off_a_thin_sample_is():
    """A tiny edge inside a lot of noise needs a lot of trades, and says so."""
    sample = ([1.0] * 26 + [-1.0] * 25) * 2       # expectancy ~ +0.01, spread 1.0
    stats = significance(sample, iterations=500)
    assert not stats.distinguishable_from_noise
    assert stats.trades_needed is not None
    assert stats.trades_needed > len(sample)
    assert str(stats.trades_needed) in stats.verdict()


def test_an_empty_sample_is_none():
    assert significance([]) is None
    assert significance([1.0]) is None


# ---- walk-forward -----------------------------------------------------------


def test_walk_forward_reports_in_and_out_of_sample_separately(settings):
    candles = make_candles(trend_closes(2000, drift=0.003))
    result = walk_forward(candles, settings, {"long_threshold": [55.0, 70.0]}, folds=3)

    assert result.folds
    assert result.in_sample_expectancy is not None
    assert result.out_of_sample_expectancy is not None
    for fold in result.folds:
        # The test block must start where the train block ends. No overlap.
        assert fold.train_bars[1] == fold.test_bars[0]


def test_each_fold_records_the_parameters_it_chose(settings):
    candles = make_candles(trend_closes(2000, drift=0.003))
    result = walk_forward(candles, settings, {"long_threshold": [55.0, 70.0]}, folds=3)
    for fold in result.folds:
        assert "long_threshold" in fold.chosen


def test_too_little_history_produces_no_folds(settings):
    assert walk_forward(make_candles(trend_closes(200)), settings,
                        {"long_threshold": [60.0]}, folds=4).folds == []


def test_the_verdict_refuses_to_conclude_from_a_thin_sample(settings):
    candles = make_candles(trend_closes(900, drift=0.003))
    result = walk_forward(candles, settings, {"long_threshold": [60.0]}, folds=2)
    if len(result.out_of_sample_trades) < 30:
        assert "too few" in result.verdict()


def test_the_summary_publishes_the_degradation(settings):
    candles = make_candles(trend_closes(2000, drift=0.003))
    summary = walk_forward(candles, settings, {"long_threshold": [55.0, 70.0]}, folds=3).to_dict()
    for key in ("in_sample_expectancy_r", "out_of_sample_expectancy_r",
                "degradation_r", "verdict", "folds"):
        assert key in summary
