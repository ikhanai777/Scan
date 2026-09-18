"""Entry zone, stop, targets and holding window for a fired signal.

Everything is derived from ATR and the nearest pivot level, so the numbers
scale with each coin's own volatility instead of assuming a fixed percentage.
The holding window is kinematic: how long the second target takes to reach at
the coin's recent pace, clamped to the spec's 15-minute-to-72-hour band.

These are suggestions on a chart, not position sizes. Nothing here knows or
wants to know an account balance.
"""

from __future__ import annotations

import math

from .config import Settings
from .features import TechnicalFeatures
from .models import Direction, Levels, timeframe_minutes

# Risk is capped here so a stop placed beyond a distant support level cannot
# quietly turn a scalp into an open-ended bet.
MAX_STOP_ATR = 2.5
MIN_STOP_ATR = 0.6
# A pivot stop sits this far past the level, to clear the wick that made it.
STOP_BUFFER_ATR = 0.25
# Price rarely travels in a straight line; it covers roughly one ATR of net
# distance every this-many bars. Used only to size the holding window.
BARS_PER_ATR_OF_PROGRESS = 2.5


def build_levels(features: TechnicalFeatures, direction: Direction, settings: Settings) -> Levels:
    atr = features.atr
    price = features.close
    if not math.isfinite(atr) or atr <= 0 or price <= 0:
        raise ValueError(f"{features.symbol}: cannot build levels without a positive ATR and price")

    sign = direction.sign
    band = settings.entry_band_atr * atr

    # The zone reaches back against the trade -- a pullback entry -- and only
    # slightly beyond current price, so chasing is bounded.
    if direction is Direction.LONG:
        entry_low, entry_high = price - band, price + 0.10 * atr
    else:
        entry_low, entry_high = price - 0.10 * atr, price + band
    entry_mid = (entry_low + entry_high) / 2.0

    stop = _stop_for(features, direction, settings, entry_mid, atr)
    risk = abs(entry_mid - stop)
    target1 = entry_mid + sign * settings.target1_r * risk
    target2 = entry_mid + sign * settings.target2_r * risk

    hold_minutes = _hold_minutes(features, risk * settings.target2_r, atr, settings)
    return Levels(
        entry_low=_round_price(entry_low), entry_high=_round_price(entry_high),
        stop=_round_price(stop), target1=_round_price(target1), target2=_round_price(target2),
        hold_minutes=hold_minutes,
    )


def _stop_for(features: TechnicalFeatures, direction: Direction, settings: Settings,
              entry_mid: float, atr: float) -> float:
    """The ATR stop, widened to clear a nearby pivot, then capped."""
    atr_stop = entry_mid - direction.sign * settings.stop_atr_multiple * atr
    floor = entry_mid - direction.sign * MAX_STOP_ATR * atr
    ceiling = entry_mid - direction.sign * MIN_STOP_ATR * atr

    level = features.support if direction is Direction.LONG else features.resistance
    if math.isfinite(level):
        pivot_stop = level - direction.sign * STOP_BUFFER_ATR * atr
        # Only honour the pivot when it sits on the losing side of the entry;
        # a "support" above a long entry is not a stop, it is a target.
        if direction.sign * (entry_mid - pivot_stop) > 0:
            atr_stop = min(atr_stop, pivot_stop) if direction is Direction.LONG else max(atr_stop, pivot_stop)

    if direction is Direction.LONG:
        return min(max(atr_stop, floor), ceiling)
    return max(min(atr_stop, floor), ceiling)


def _hold_minutes(features: TechnicalFeatures, distance: float, atr: float, settings: Settings) -> int:
    """How long the far target plausibly takes at this coin's recent pace."""
    minutes_per_bar = timeframe_minutes(features.timeframe)
    atrs_to_travel = distance / atr if atr > 0 else settings.target2_r
    bars = atrs_to_travel * BARS_PER_ATR_OF_PROGRESS
    # A market whose ATR is already expanding gets there sooner.
    if math.isfinite(features.atr_expansion) and features.atr_expansion > 1.0:
        bars /= min(features.atr_expansion, 1.6)
    minutes = int(round(bars * minutes_per_bar))
    return max(settings.min_hold_minutes, min(settings.max_hold_minutes, minutes))


def _round_price(value: float) -> float:
    """Round to a sane tick for the magnitude, so cards do not print 17 digits.

    This is display precision, not exchange precision -- v1 places no orders,
    so there is no tick size to honour, only a number a human has to read.
    """
    magnitude = abs(value)
    if magnitude >= 1000:
        return round(value, 2)
    if magnitude >= 1:
        return round(value, 4)
    if magnitude >= 0.01:
        return round(value, 6)
    return float(f"{value:.6g}")
