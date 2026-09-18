"""Indicator maths, checked against hand-computable cases and known properties."""

from __future__ import annotations

import numpy as np
import pytest

from cryptosignal import indicators as ind


def test_sma_matches_manual_mean():
    values = [1, 2, 3, 4, 5, 6]
    result = ind.sma(values, 3)
    assert np.isnan(result[:2]).all()
    assert result[2] == pytest.approx(2.0)
    assert result[-1] == pytest.approx(5.0)


def test_sma_is_shorter_than_period_all_nan():
    assert np.isnan(ind.sma([1, 2], 5)).all()


def test_ema_seeds_on_the_mean_then_tracks():
    values = np.arange(1, 21, dtype=float)
    result = ind.ema(values, 5)
    assert result[4] == pytest.approx(3.0)              # mean of 1..5
    # A rising series pulls the EMA up but keeps it behind price.
    assert result[-1] < values[-1]
    assert result[-1] > result[-2]


def test_ema_of_a_constant_is_the_constant():
    result = ind.ema(np.full(50, 7.0), 10)
    assert result[-1] == pytest.approx(7.0)


def test_rsi_saturates_on_a_monotonic_rise():
    result = ind.rsi(np.arange(1, 60, dtype=float), 14)
    assert result[-1] == pytest.approx(100.0)


def test_rsi_bottoms_on_a_monotonic_fall():
    result = ind.rsi(np.arange(60, 1, -1, dtype=float), 14)
    assert result[-1] == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_neutral():
    """No gains and no losses is neither overbought nor oversold."""
    result = ind.rsi(np.full(60, 42.0), 14)
    assert result[-1] == pytest.approx(50.0)


def test_rsi_stays_in_range(rng_seed=3):
    rng = np.random.default_rng(rng_seed)
    series = 100 + np.cumsum(rng.normal(0, 1, 300))
    result = ind.rsi(series, 14)
    finite = result[np.isfinite(result)]
    assert finite.size > 0
    assert finite.min() >= 0.0 and finite.max() <= 100.0


def test_macd_histogram_is_positive_in_an_uptrend():
    series = np.cumprod(np.full(120, 1.004)) * 100
    _, _, hist = ind.macd(series)
    assert hist[-1] > 0


def test_macd_signal_line_starts_where_the_macd_line_does():
    series = np.cumprod(np.full(120, 1.004)) * 100
    line, signal, _ = ind.macd(series)
    first_line = int(np.argmax(~np.isnan(line)))
    first_signal = int(np.argmax(~np.isnan(signal)))
    assert first_signal >= first_line
    # The signal EMA must not be seeded on the nan warm-up of the MACD line.
    assert np.isfinite(signal[first_signal])


def test_stochastic_is_bounded_and_flat_window_is_midpoint():
    high = np.full(40, 10.0)
    low = np.full(40, 10.0)
    close = np.full(40, 10.0)
    k, d = ind.stochastic(high, low, close)
    assert k[-1] == pytest.approx(50.0)
    assert d[-1] == pytest.approx(50.0)


def test_true_range_uses_the_previous_close():
    high = np.array([10.0, 12.0])
    low = np.array([9.0, 11.5])
    close = np.array([9.5, 12.0])
    tr = ind.true_range(high, low, close)
    # Bar 2: high-low = 0.5, high-prev_close = 2.5, low-prev_close = 2.0.
    assert tr[1] == pytest.approx(2.5)


def test_atr_is_positive_and_scales_with_volatility():
    calm_close = 100 + np.sin(np.arange(200) / 5) * 0.5
    wild_close = 100 + np.sin(np.arange(200) / 5) * 5.0
    calm = ind.atr(calm_close + 0.1, calm_close - 0.1, calm_close)
    wild = ind.atr(wild_close + 1.0, wild_close - 1.0, wild_close)
    assert calm[-1] > 0
    assert wild[-1] > calm[-1]


def test_bollinger_width_widens_with_dispersion():
    tight = np.full(60, 100.0) + np.random.default_rng(1).normal(0, 0.05, 60)
    loose = np.full(60, 100.0) + np.random.default_rng(1).normal(0, 2.0, 60)
    _, _, _, tight_width = ind.bollinger(tight)
    _, _, _, loose_width = ind.bollinger(loose)
    assert loose_width[-1] > tight_width[-1]


def test_obv_accumulates_on_up_bars_only():
    close = np.array([10.0, 11.0, 10.5, 12.0])
    volume = np.array([100.0, 200.0, 50.0, 300.0])
    result = ind.obv(close, volume)
    assert result[0] == 0.0
    assert result[1] == pytest.approx(200.0)
    assert result[2] == pytest.approx(150.0)
    assert result[3] == pytest.approx(450.0)


def test_adx_is_high_in_a_trend_and_low_in_a_range():
    trend = np.cumprod(np.full(200, 1.006)) * 100
    ranging = 100 + np.sin(np.arange(200) / 4) * 1.0
    trend_adx, plus_di, minus_di = ind.adx(trend + 0.2, trend - 0.2, trend)
    range_adx, _, _ = ind.adx(ranging + 0.2, ranging - 0.2, ranging)
    assert trend_adx[-1] > range_adx[-1]
    assert plus_di[-1] > minus_di[-1]


def test_adx_does_not_seed_on_the_warmup():
    """Zero-filling the DX warm-up would drag the first ADX values toward zero."""
    trend = np.cumprod(np.full(200, 1.006)) * 100
    adx, _, _ = ind.adx(trend + 0.2, trend - 0.2, trend)
    first = int(np.argmax(np.isfinite(adx)))
    assert adx[first] > 20.0


def test_rolling_vwap_sits_between_the_extremes():
    close = np.array([10.0] * 30 + [20.0] * 30)
    volume = np.full(60, 100.0)
    result = ind.rolling_vwap(close + 0.1, close - 0.1, close, volume, period=48)
    assert 10.0 < result[-1] < 20.0


def test_rolling_vwap_survives_a_zero_volume_window():
    close = np.linspace(10, 12, 60)
    volume = np.zeros(60)
    result = ind.rolling_vwap(close + 0.1, close - 0.1, close, volume, period=48)
    assert np.isfinite(result[-1])


def test_relative_volume_excludes_the_current_bar_from_its_own_baseline():
    volume = np.concatenate([np.full(40, 100.0), [1000.0]])
    result = ind.relative_volume(volume, 20)
    assert result[-1] == pytest.approx(10.0)


def test_swing_highs_need_confirmation_bars():
    """The final bars can never be pivots -- a level needs price to leave it."""
    high = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3, 9], dtype=float)
    mask = ind.swing_highs(high, 3, 3)
    assert mask[3]
    assert not mask[-1]


def test_linreg_slope_signs_match_direction():
    assert ind.linreg_slope(np.arange(20, dtype=float), 10) == pytest.approx(1.0)
    assert ind.linreg_slope(np.arange(20, 0, -1, dtype=float), 10) == pytest.approx(-1.0)


def test_percentile_rank_ignores_nan():
    values = np.array([1.0, np.nan, 2.0, 3.0])
    assert ind.percentile_rank(values, 2.0) == pytest.approx(2 / 3)


def test_every_indicator_returns_input_length():
    """Callers index [-1] for 'now'; a shortened array would silently misalign."""
    size = 120
    close = np.cumprod(np.full(size, 1.002)) * 50
    high, low = close * 1.002, close * 0.998
    volume = np.full(size, 10.0)
    for result in (
        ind.sma(close, 20), ind.ema(close, 20), ind.wilder(close, 14),
        ind.rsi(close), ind.atr(high, low, close), ind.obv(close, volume),
        ind.relative_volume(volume), ind.rolling_vwap(high, low, close, volume),
    ):
        assert result.shape == (size,)
    for group in (ind.macd(close), ind.stochastic(high, low, close), ind.adx(high, low, close),
                  ind.bollinger(close)):
        for result in group:
            assert result.shape == (size,)
