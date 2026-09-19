"""What kind of market is this, and does the higher timeframe agree?

Two filters that desks apply before they look at a setup at all, and that this
engine was missing. Both are pure functions of candles already fetched or one
extra fetch, and both are keyless.

**Higher-timeframe confluence.** Taking a 15-minute long into a 4-hour
downtrend is the most reliable way to lose money with a working indicator set.
The lower timeframe is where you time the entry; the higher one decides whether
you should be looking for that entry at all. This does not hard-veto -- a
counter-trend signal into a *weak* higher trend is a real trade -- it scales
the score down, and vetoes only when the higher trend is both strong and
opposed.

**Regime.** The same indicators mean different things in different markets. A
break of resistance in a trending market is continuation; the same break in a
chop is the top of the range. Kaufman's **efficiency ratio** is the cleanest
single measure: net distance travelled divided by the total path walked to get
there. A market that moved 10% in a straight line has an ER near 1; one that
moved 10% via 80% of wandering has an ER near 0.1. Combined with ADX it
separates trend from range far better than either alone.

The regime is reported on every card rather than only used internally, because
"this fired in a chop" is something a trader wants to see before sizing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from . import indicators as ind
from .config import Settings
from .models import Candles
from .scoring import clamp

#: Kaufman's efficiency ratio over this many bars.
EFFICIENCY_BARS = 20
#: Above this the path is directional enough to call a trend.
TRENDING_EFFICIENCY = 0.35
CHOPPY_EFFICIENCY = 0.15
#: ADX thresholds, the classic readings.
ADX_TRENDING = 25.0
ADX_WEAK = 18.0
#: Realised volatility above this percentile of its own history is "volatile".
VOLATILE_PERCENTILE = 0.80


class Regime(StrEnum):
    TRENDING = "trending"
    RANGING = "ranging"
    CHOPPY = "choppy"
    VOLATILE = "volatile"

    @property
    def is_tradeable(self) -> bool:
        """Chop is where indicator systems bleed. It is the one to sit out."""
        return self is not Regime.CHOPPY


@dataclass(frozen=True)
class RegimeReading:
    regime: Regime
    efficiency: float
    adx: float
    volatility_percentile: float
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "regime": self.regime.value,
            "efficiency": round(self.efficiency, 3),
            "adx": round(self.adx, 1) if math.isfinite(self.adx) else None,
            "volatility_percentile": round(self.volatility_percentile, 2),
            "detail": self.detail,
        }


def efficiency_ratio(close, period: int = EFFICIENCY_BARS) -> float:
    """Net distance over total path. 1.0 is a straight line, 0.0 is a treadmill."""
    arr = np.asarray(close, dtype=float)
    if arr.size <= period:
        return float("nan")
    window = arr[-(period + 1):]
    net = abs(window[-1] - window[0])
    path = float(np.abs(np.diff(window)).sum())
    if path <= 0:
        return 0.0
    return float(net / path)


def classify(candles: Candles) -> RegimeReading | None:
    """Name the market this is, from its own recent behaviour."""
    if len(candles) < EFFICIENCY_BARS + 30:
        return None

    close = np.asarray(candles.close, dtype=float)
    high = np.asarray(candles.high, dtype=float)
    low = np.asarray(candles.low, dtype=float)

    efficiency = efficiency_ratio(close)
    adx_series, _, _ = ind.adx(high, low, close)
    adx = float(adx_series[-1]) if np.isfinite(adx_series[-1]) else float("nan")

    # Realised volatility against its own history, so "volatile" means volatile
    # for this coin rather than volatile compared to a blue chip.
    returns = np.diff(close) / close[:-1]
    window = returns[-EFFICIENCY_BARS:]
    realised = float(np.std(window)) if window.size else float("nan")
    history = np.array([
        float(np.std(returns[i - EFFICIENCY_BARS:i]))
        for i in range(EFFICIENCY_BARS, returns.size + 1)
    ]) if returns.size >= EFFICIENCY_BARS else np.array([])
    percentile = ind.percentile_rank(history, realised) if history.size else float("nan")

    strong_adx = math.isfinite(adx) and adx >= ADX_TRENDING
    weak_adx = math.isfinite(adx) and adx < ADX_WEAK

    if math.isfinite(efficiency) and efficiency >= TRENDING_EFFICIENCY and not weak_adx:
        regime = Regime.TRENDING
        detail = f"directional path (efficiency {efficiency:.2f}, ADX {adx:.0f})"
    elif math.isfinite(percentile) and percentile >= VOLATILE_PERCENTILE and not strong_adx:
        # Big moves going nowhere: expensive stops, unreliable levels.
        regime = Regime.VOLATILE
        detail = f"high volatility without direction (efficiency {efficiency:.2f})"
    elif math.isfinite(efficiency) and efficiency <= CHOPPY_EFFICIENCY:
        regime = Regime.CHOPPY
        detail = f"price going nowhere (efficiency {efficiency:.2f}, ADX {adx:.0f})"
    else:
        regime = Regime.RANGING
        detail = f"range-bound (efficiency {efficiency:.2f}, ADX {adx:.0f})"

    return RegimeReading(regime, efficiency, adx, percentile, detail)


# ---- higher-timeframe confluence -------------------------------------------


@dataclass(frozen=True)
class HigherTimeframe:
    """The trend on the timeframe above, and how strongly it holds."""

    timeframe: str
    direction: int                # +1 up, -1 down, 0 undecided
    strength: float               # 0..1
    detail: str

    def alignment(self, signal_direction: int) -> float:
        """+1 fully aligned, -1 fully opposed, scaled by how strong the trend is."""
        if self.direction == 0:
            return 0.0
        return float(self.direction * signal_direction * self.strength)

    def to_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "direction": self.direction,
            "strength": round(self.strength, 2),
            "detail": self.detail,
        }


def higher_timeframe_trend(candles: Candles) -> HigherTimeframe | None:
    """Read the trend on an already-fetched higher-timeframe series.

    Deliberately simple and slow-moving: EMA stack plus where price sits
    against the long EMA, scaled by ADX. A higher-timeframe filter that flips
    every other bar is not a filter.
    """
    if len(candles) < 60:
        return None

    close = np.asarray(candles.close, dtype=float)
    high = np.asarray(candles.high, dtype=float)
    low = np.asarray(candles.low, dtype=float)

    fast = ind.ema(close, 21)[-1]
    slow = ind.ema(close, 55)[-1]
    if not (math.isfinite(fast) and math.isfinite(slow)):
        return None

    adx_series, _, _ = ind.adx(high, low, close)
    adx = float(adx_series[-1]) if np.isfinite(adx_series[-1]) else float("nan")

    stacked = 1 if fast > slow else -1
    above = 1 if close[-1] > slow else -1
    if stacked != above:
        # Price and the stack disagree: the higher timeframe is turning, and
        # an undecided filter should say so rather than pick a side.
        return HigherTimeframe(candles.timeframe, 0, 0.0,
                               f"{candles.timeframe} trend turning (price and EMAs disagree)")

    separation = abs(fast - slow) / slow if slow > 0 else 0.0
    adx_strength = clamp(adx / ADX_TRENDING, 0.0, 1.0) if math.isfinite(adx) else 0.5
    strength = float(min(1.0, (min(separation / 0.02, 1.0) * 0.5) + adx_strength * 0.5))

    word = "up" if stacked > 0 else "down"
    return HigherTimeframe(
        candles.timeframe, stacked, strength,
        f"{candles.timeframe} trend {word} (strength {strength:.2f}, ADX {adx:.0f})",
    )


def higher_timeframe_for(timeframe: str, settings: Settings) -> str:
    """The timeframe to check above this one, from the configured ladder."""
    ladder = settings.higher_timeframe_ladder
    return ladder.get(timeframe, settings.default_higher_timeframe)


def apply_confluence(score: float, higher: HigherTimeframe | None,
                     settings: Settings) -> tuple[float, str]:
    """Scale a directional score by higher-timeframe agreement.

    Returns the adjusted score and a one-line reason. An opposed *and strong*
    higher trend can veto outright; an opposed weak one only costs size, because
    a genuine reversal always starts as a counter-trend signal and a hard veto
    would miss every one of them.
    """
    if higher is None or score == 0.0:
        return score, ""

    signal_direction = 1 if score > 0 else -1
    alignment = higher.alignment(signal_direction)

    if alignment >= 0:
        boost = 1.0 + settings.htf_aligned_bonus * alignment
        return clamp(score * boost), (
            f"{higher.timeframe} trend agrees" if alignment > 0.2 else ""
        )

    opposed = abs(alignment)
    if opposed >= settings.htf_veto_strength:
        return 0.0, f"vetoed: {higher.timeframe} trend is strongly opposed ({opposed:.2f})"

    penalty = 1.0 - settings.htf_opposed_penalty * opposed
    return clamp(score * penalty), f"{higher.timeframe} trend opposed, score cut {1 - penalty:.0%}"
