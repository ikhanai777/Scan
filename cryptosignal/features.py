"""One pass over a coin's candles, producing every number the scoring needs.

Both stage 2 of the screen and the technical leg want ATR, relative volume and
the nearest structure levels. Computing them once, here, means the two stages
can never disagree about what the chart is doing -- and the deep analysis costs
no extra indicator passes over what the screen already paid for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import indicators as ind
from .models import Candles

# Enough bars for a 200 EMA plus its seed. Below this the long-trend read is
# absent and the trend component falls back to the shorter stack.
MIN_BARS = 60
EMA_PERIODS = (9, 21, 50, 200)
ATR_PERIOD = 14
BB_PERIOD = 20
RVOL_PERIOD = 20
VWAP_PERIOD = 48
OBV_SLOPE_BARS = 14
# How far back a break of structure still counts as "fresh".
BREAK_LOOKBACK = 6
SWING_LEFT = 3
SWING_RIGHT = 3


def _last(array: np.ndarray) -> float:
    """The most recent value, or nan if the indicator never warmed up."""
    if array.size == 0:
        return float("nan")
    value = float(array[-1])
    return value if math.isfinite(value) else float("nan")


@dataclass(frozen=True)
class TechnicalFeatures:
    symbol: str
    timeframe: str
    bars: int
    close: float

    ema9: float
    ema21: float
    ema50: float
    ema200: float

    adx: float
    plus_di: float
    minus_di: float

    rsi: float
    macd_hist: float
    macd_hist_prev: float
    macd_scale: float           # ATR, used to make the histogram comparable across coins
    stoch_k: float
    stoch_d: float

    atr: float
    atr_pct: float              # ATR as a percent of price
    atr_expansion: float        # current ATR vs its own trailing median
    bb_width: float
    bb_width_median: float
    bb_position: float          # -1 at the lower band, +1 at the upper

    obv_slope_norm: float       # OBV slope per bar, in units of average volume
    rvol: float                 # last bar's volume vs its trailing average

    vwap: float
    vwap_distance_atr: float    # (close - vwap) / ATR

    resistance: float           # nearest confirmed swing high above/at price
    support: float              # nearest confirmed swing low below/at price
    broke_resistance: bool
    broke_support: bool
    distance_to_resistance_atr: float
    distance_to_support_atr: float

    @property
    def has_long_trend(self) -> bool:
        return math.isfinite(self.ema200)


def compute_features(candles: Candles) -> TechnicalFeatures | None:
    """Returns None when the coin has too little history to score honestly."""
    if len(candles) < MIN_BARS:
        return None

    high = np.asarray(candles.high, dtype=float)
    low = np.asarray(candles.low, dtype=float)
    close = np.asarray(candles.close, dtype=float)
    volume = np.asarray(candles.volume, dtype=float)

    emas = {period: _last(ind.ema(close, period)) if close.size >= period else float("nan")
            for period in EMA_PERIODS}

    adx_series, plus_di, minus_di = ind.adx(high, low, close)
    rsi_series = ind.rsi(close)
    _, _, macd_hist = ind.macd(close)
    stoch_k, stoch_d = ind.stochastic(high, low, close)

    atr_series = ind.atr(high, low, close, ATR_PERIOD)
    atr_now = _last(atr_series)
    # "Volatility expansion" from the spec's stage 2: today's ATR against the
    # median of the last 50 bars' ATR. A median, not a mean, so one spike bar
    # does not define the baseline it is supposed to stand out from.
    atr_history = atr_series[-50:]
    atr_history = atr_history[np.isfinite(atr_history)]
    atr_baseline = float(np.median(atr_history)) if atr_history.size >= 10 else float("nan")
    atr_expansion = atr_now / atr_baseline if atr_baseline and math.isfinite(atr_baseline) and atr_baseline > 0 else float("nan")

    bb_mid, bb_upper, bb_lower, bb_width = ind.bollinger(close, BB_PERIOD)
    width_now = _last(bb_width)
    width_history = bb_width[-50:]
    width_history = width_history[np.isfinite(width_history)]
    width_median = float(np.median(width_history)) if width_history.size >= 10 else float("nan")
    half_span = (_last(bb_upper) - _last(bb_mid))
    bb_position = (close[-1] - _last(bb_mid)) / half_span if half_span and math.isfinite(half_span) and half_span > 0 else 0.0
    bb_position = float(np.clip(bb_position, -2.0, 2.0))

    obv_series = ind.obv(close, volume)
    obv_slope = ind.linreg_slope(obv_series, OBV_SLOPE_BARS)
    avg_volume = float(np.mean(volume[-RVOL_PERIOD:])) if volume.size >= RVOL_PERIOD else float(np.mean(volume))
    obv_slope_norm = obv_slope / avg_volume if avg_volume > 0 and math.isfinite(obv_slope) else float("nan")

    rvol = _last(ind.relative_volume(volume, RVOL_PERIOD))

    vwap_series = ind.rolling_vwap(high, low, close, volume, VWAP_PERIOD)
    vwap_now = _last(vwap_series)
    vwap_distance_atr = ((close[-1] - vwap_now) / atr_now
                         if math.isfinite(vwap_now) and math.isfinite(atr_now) and atr_now > 0
                         else float("nan"))

    resistance, support, broke_up, broke_down = _structure(high, low, close)
    to_resistance = ((resistance - close[-1]) / atr_now
                     if math.isfinite(resistance) and math.isfinite(atr_now) and atr_now > 0
                     else float("nan"))
    to_support = ((close[-1] - support) / atr_now
                  if math.isfinite(support) and math.isfinite(atr_now) and atr_now > 0
                  else float("nan"))

    return TechnicalFeatures(
        symbol=candles.symbol,
        timeframe=candles.timeframe,
        bars=len(candles),
        close=float(close[-1]),
        ema9=emas[9], ema21=emas[21], ema50=emas[50], ema200=emas[200],
        adx=_last(adx_series), plus_di=_last(plus_di), minus_di=_last(minus_di),
        rsi=_last(rsi_series),
        macd_hist=_last(macd_hist),
        macd_hist_prev=float(macd_hist[-2]) if macd_hist.size >= 2 and math.isfinite(macd_hist[-2]) else float("nan"),
        macd_scale=atr_now,
        stoch_k=_last(stoch_k), stoch_d=_last(stoch_d),
        atr=atr_now,
        atr_pct=100.0 * atr_now / close[-1] if math.isfinite(atr_now) and close[-1] > 0 else float("nan"),
        atr_expansion=atr_expansion,
        bb_width=width_now,
        bb_width_median=width_median,
        bb_position=bb_position,
        obv_slope_norm=obv_slope_norm,
        rvol=rvol,
        vwap=vwap_now,
        vwap_distance_atr=vwap_distance_atr,
        resistance=resistance,
        support=support,
        broke_resistance=broke_up,
        broke_support=broke_down,
        distance_to_resistance_atr=to_resistance,
        distance_to_support_atr=to_support,
    )


def _structure(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> tuple[float, float, bool, bool]:
    """Nearest confirmed pivot levels, and whether price just broke one.

    A pivot needs `SWING_RIGHT` bars of confirmation, so the levels here are
    always at least that old -- which is what makes them levels rather than
    just the recent extreme. A break is only counted when price was on the
    other side of the level within the last `BREAK_LOOKBACK` bars; an old
    breakout that has since gone quiet is not a fresh trigger.
    """
    highs_mask = ind.swing_highs(high, SWING_LEFT, SWING_RIGHT)
    lows_mask = ind.swing_lows(low, SWING_LEFT, SWING_RIGHT)
    price = float(close[-1])

    high_levels = high[highs_mask]
    low_levels = low[lows_mask]

    above = high_levels[high_levels >= price]
    resistance = float(above.min()) if above.size else (float(high_levels.max()) if high_levels.size else float("nan"))
    below = low_levels[low_levels <= price]
    support = float(below.max()) if below.size else (float(low_levels.min()) if low_levels.size else float("nan"))

    # The level price is breaking is the last one it was on the other side of:
    # for an upside break, the highest confirmed pivot high that price has now
    # cleared but had not cleared `BREAK_LOOKBACK` bars ago.
    window = close[-BREAK_LOOKBACK:] if close.size >= BREAK_LOOKBACK else close
    cleared_highs = high_levels[high_levels < price]
    broke_up = bool(cleared_highs.size and (window.min() <= cleared_highs.max() < price))
    cleared_lows = low_levels[low_levels > price]
    broke_down = bool(cleared_lows.size and (price < cleared_lows.min() <= window.max()))

    return resistance, support, broke_up, broke_down
