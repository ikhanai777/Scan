"""Indicator maths, as plain array in / array out functions.

Every function returns an array the same length as its input, with `nan` over
the warm-up period rather than a shortened array. Keeping the alignment means
callers can always index `[-1]` for "now" without tracking per-indicator
offsets, and a `nan` there is an honest "not enough history yet" that the
scoring layer can test for.

Wilder's smoothing (RSI, ATR, ADX) is the classic recursive form, not a simple
mean, so values match what a trader reads off TradingView.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sma", "ema", "wilder", "rsi", "macd", "stochastic", "true_range", "atr",
    "bollinger", "obv", "adx", "rolling_vwap", "relative_volume",
    "swing_highs", "swing_lows", "linreg_slope", "percentile_rank",
]


def _as_array(values) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise ValueError("indicator inputs must be one-dimensional")
    return arr


def sma(values, period: int) -> np.ndarray:
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if period <= 0:
        raise ValueError("period must be positive")
    if arr.size < period:
        return out
    cumulative = np.cumsum(np.insert(arr, 0, 0.0))
    out[period - 1:] = (cumulative[period:] - cumulative[:-period]) / period
    return out


def ema(values, period: int) -> np.ndarray:
    """Seeded with the first `period` values' mean, then recursive."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if period <= 0:
        raise ValueError("period must be positive")
    if arr.size < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = arr[:period].mean()
    for i in range(period, arr.size):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def wilder(values, period: int) -> np.ndarray:
    """Wilder's smoothing: an EMA with alpha = 1/period, seeded on the mean."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if period <= 0:
        raise ValueError("period must be positive")
    if arr.size < period:
        return out
    out[period - 1] = arr[:period].mean()
    for i in range(period, arr.size):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return out


def rsi(close, period: int = 14) -> np.ndarray:
    arr = _as_array(close)
    out = np.full(arr.shape, np.nan)
    if arr.size <= period:
        return out
    delta = np.diff(arr, prepend=arr[0])
    delta[0] = 0.0
    gains = np.clip(delta, 0.0, None)
    losses = np.clip(-delta, 0.0, None)
    # Wilder seeds on the first `period` deltas, which live at indices 1..period.
    avg_gain = wilder(gains[1:], period)
    avg_loss = wilder(losses[1:], period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, np.inf)
        values = 100.0 - 100.0 / (1.0 + rs)
    # No losses at all is RSI 100 -- but no movement at all is neither
    # overbought nor oversold, so a dead-flat stretch reads 50.
    flat = (avg_gain == 0) & (avg_loss == 0)
    values = np.where(flat, 50.0, values)
    out[1:] = values
    return out


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    arr = _as_array(close)
    fast_ema, slow_ema = ema(arr, fast), ema(arr, slow)
    line = fast_ema - slow_ema
    # The signal EMA must start where the MACD line starts, not at index 0.
    valid = ~np.isnan(line)
    signal_line = np.full(arr.shape, np.nan)
    if valid.any():
        start = int(np.argmax(valid))
        signal_line[start:] = ema(line[start:], signal)
    return line, signal_line, line - signal_line


def stochastic(high, low, close, k_period: int = 14, d_period: int = 3):
    """Fast %K with an SMA %D. Returns (k, d), both 0..100."""
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    out_k = np.full(c.shape, np.nan)
    if c.size < k_period:
        return out_k, np.full(c.shape, np.nan)
    for i in range(k_period - 1, c.size):
        window_high = h[i - k_period + 1: i + 1].max()
        window_low = lo[i - k_period + 1: i + 1].min()
        span = window_high - window_low
        # A perfectly flat window is the midpoint, not a divide-by-zero.
        out_k[i] = 50.0 if span <= 0 else 100.0 * (c[i] - window_low) / span
    out_d = np.full(c.shape, np.nan)
    start = k_period - 1
    out_d[start:] = sma(out_k[start:], d_period)
    return out_k, out_d


def true_range(high, low, close) -> np.ndarray:
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    return np.maximum.reduce([h - lo, np.abs(h - prev_close), np.abs(lo - prev_close)])


def atr(high, low, close, period: int = 14) -> np.ndarray:
    return wilder(true_range(high, low, close), period)


def bollinger(close, period: int = 20, deviations: float = 2.0):
    """Returns (middle, upper, lower, width) where width is normalised by the middle."""
    arr = _as_array(close)
    middle = sma(arr, period)
    std = np.full(arr.shape, np.nan)
    for i in range(period - 1, arr.size):
        std[i] = arr[i - period + 1: i + 1].std()
    upper = middle + deviations * std
    lower = middle - deviations * std
    with np.errstate(divide="ignore", invalid="ignore"):
        width = np.where(middle > 0, (upper - lower) / middle, np.nan)
    return middle, upper, lower, width


def obv(close, volume) -> np.ndarray:
    """On-balance volume, starting at zero on the first bar."""
    c, v = _as_array(close), _as_array(volume)
    out = np.zeros(c.shape)
    direction = np.sign(np.diff(c))
    out[1:] = np.cumsum(direction * v[1:])
    return out


def adx(high, low, close, period: int = 14):
    """Returns (adx, plus_di, minus_di)."""
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    size = c.size
    nan = np.full(c.shape, np.nan)
    if size < period * 2:
        return nan, nan.copy(), nan.copy()

    up_move = np.diff(h, prepend=h[0])
    down_move = -np.diff(lo, prepend=lo[0])
    up_move[0] = down_move[0] = 0.0
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = wilder(true_range(h, lo, c), period)
    plus_smooth = wilder(plus_dm, period)
    minus_smooth = wilder(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(tr_smooth > 0, 100.0 * plus_smooth / tr_smooth, 0.0)
        minus_di = np.where(tr_smooth > 0, 100.0 * minus_smooth / tr_smooth, 0.0)
        di_sum = plus_di + minus_di
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)

    dx = np.where(np.isnan(tr_smooth), np.nan, dx)
    plus_di = np.where(np.isnan(tr_smooth), np.nan, plus_di)
    minus_di = np.where(np.isnan(tr_smooth), np.nan, minus_di)

    # ADX is Wilder's smoothing of DX, and DX itself only starts once the
    # smoothed TR does. Starting the second smoothing at the first finite DX
    # -- rather than zero-filling the warm-up -- keeps the seed honest.
    out = np.full(c.shape, np.nan)
    finite_positions = np.flatnonzero(np.isfinite(dx))
    if finite_positions.size >= period:
        first = int(finite_positions[0])
        out[first:] = wilder(dx[first:], period)
    return out, plus_di, minus_di


def rolling_vwap(high, low, close, volume, period: int = 48) -> np.ndarray:
    """Volume-weighted average of the typical price over a rolling window.

    A true session VWAP needs a session boundary, and crypto has no close, so
    the rolling form is the honest analogue: `period` bars of the same weight
    the exchange gave them.
    """
    h, lo, c, v = _as_array(high), _as_array(low), _as_array(close), _as_array(volume)
    typical = (h + lo + c) / 3.0
    out = np.full(c.shape, np.nan)
    if c.size < period:
        return out
    pv = typical * v
    cum_pv = np.cumsum(np.insert(pv, 0, 0.0))
    cum_v = np.cumsum(np.insert(v, 0, 0.0))
    window_pv = cum_pv[period:] - cum_pv[:-period]
    window_v = cum_v[period:] - cum_v[:-period]
    window_typical_mean = sma(typical, period)[period - 1:]
    # Zero volume over a whole window means the venue went quiet; fall back to
    # the unweighted mean rather than emitting nan for an otherwise live coin.
    out[period - 1:] = np.where(window_v > 0, window_pv / np.where(window_v > 0, window_v, 1.0), window_typical_mean)
    return out


def relative_volume(volume, period: int = 20) -> np.ndarray:
    """Each bar's volume as a multiple of the trailing average, excluding itself."""
    v = _as_array(volume)
    out = np.full(v.shape, np.nan)
    if v.size <= period:
        return out
    baseline = sma(v, period)
    prior = np.roll(baseline, 1)
    prior[0] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(prior > 0, v / prior, np.nan)
    return out


def swing_highs(high, left: int = 3, right: int = 3) -> np.ndarray:
    """Boolean mask of pivot highs, confirmed `right` bars later.

    The last `right` bars can never be pivots -- that is the point. A level is
    only resistance once price has failed at it and moved away.
    """
    h = _as_array(high)
    mask = np.zeros(h.shape, dtype=bool)
    for i in range(left, h.size - right):
        window = h[i - left: i + right + 1]
        if h[i] == window.max() and (window == h[i]).sum() == 1:
            mask[i] = True
    return mask


def swing_lows(low, left: int = 3, right: int = 3) -> np.ndarray:
    lo = _as_array(low)
    mask = np.zeros(lo.shape, dtype=bool)
    for i in range(left, lo.size - right):
        window = lo[i - left: i + right + 1]
        if lo[i] == window.min() and (window == lo[i]).sum() == 1:
            mask[i] = True
    return mask


def linreg_slope(values, period: int) -> float:
    """Least-squares slope of the last `period` points, per bar."""
    arr = _as_array(values)
    if arr.size < period or period < 2:
        return float("nan")
    window = arr[-period:]
    if not np.isfinite(window).all():
        return float("nan")
    x = np.arange(period, dtype=float)
    x_centred = x - x.mean()
    denominator = (x_centred ** 2).sum()
    if denominator <= 0:
        return float("nan")
    return float((x_centred * (window - window.mean())).sum() / denominator)


def percentile_rank(values, value: float) -> float:
    """Share of `values` at or below `value`, as 0..1. Ignores nan."""
    arr = _as_array(values)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0 or not np.isfinite(value):
        return float("nan")
    return float((finite <= value).sum() / finite.size)
