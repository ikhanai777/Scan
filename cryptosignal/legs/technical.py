"""The technical leg: trend, momentum, volatility, volume and structure.

Carries ~50% of the fused score per the spec. Each of the five components
reports on the same -100..+100 scale and explains itself in one line, because
the signal card has to show the top three factors that drove the call -- a
score with no attached reason is not decision support.

A component whose inputs never warmed up reports weight 0 rather than a
neutral 0 score. The difference matters: a missing component should not drag
a strong reading toward the middle, it should just stop voting.
"""

from __future__ import annotations

import math

from ..config import Settings
from ..features import TechnicalFeatures
from ..models import Driver, LegName, LegScore
from ..scoring import clamp, is_usable, saturating, weighted

# Below this ADX the market has no trend worth trading, so the trend component
# is scaled down rather than asserting a direction from EMA order alone.
ADX_NO_TREND = 15.0
ADX_TRENDING = 25.0
# Oscillator readings beyond these are exhaustion: still directional, but a
# worse entry, so the score decays toward EXHAUSTION_FLOOR as the reading gets
# more extreme rather than saturating at full marks. The curve peaks in the
# strong-but-not-stretched zone, which is where the entry actually is.
RSI_EXHAUSTED_HIGH = 78.0
RSI_EXHAUSTED_LOW = 22.0
STOCH_EXHAUSTED_HIGH = 85.0
STOCH_EXHAUSTED_LOW = 15.0
EXHAUSTION_FLOOR = 0.5
# A component needs at least this many of the five to vote for the leg to count.
MIN_COMPONENTS = 3


def score_technical(features: TechnicalFeatures | None, settings: Settings) -> LegScore:
    if features is None:
        return LegScore(
            leg=LegName.TECHNICAL, score=0.0, available=False,
            note="not enough candle history to score",
        )

    components = [
        _trend(features, settings.tech_w_trend),
        _momentum(features, settings.tech_w_momentum),
        _volatility(features, settings.tech_w_volatility),
        _volume(features, settings.tech_w_volume),
        _structure(features, settings.tech_w_structure),
    ]
    voting = [d for d in components if d.weight > 0]
    if len(voting) < MIN_COMPONENTS:
        return LegScore(
            leg=LegName.TECHNICAL, score=0.0, drivers=tuple(components), available=False,
            note=f"only {len(voting)} of 5 technical components had usable data",
        )

    score = weighted([(d.score, d.weight) for d in voting])
    note = "" if len(voting) == 5 else f"{5 - len(voting)} component(s) sat out on missing data"
    return LegScore(leg=LegName.TECHNICAL, score=score, drivers=tuple(components), note=note)


def _trend(f: TechnicalFeatures, weight: float) -> Driver:
    """EMA stack ordering, distance from the long EMA, and DI spread -- scaled by ADX."""
    pairs = [(f.ema9, f.ema21), (f.ema21, f.ema50)]
    if f.has_long_trend:
        pairs.append((f.ema50, f.ema200))
    usable_pairs = [(a, b) for a, b in pairs if is_usable(a, b)]
    if not usable_pairs:
        return Driver("Trend", 0.0, 0.0, "EMAs have not warmed up")

    ordered = sum(1 if a > b else -1 for a, b in usable_pairs)
    stack = 100.0 * ordered / len(usable_pairs)

    anchor = f.ema200 if f.has_long_trend else f.ema50
    if is_usable(anchor, f.atr) and f.atr > 0:
        distance = saturating((f.close - anchor) / f.atr, full_scale=2.0)
        anchor_label = "200EMA" if f.has_long_trend else "50EMA"
    else:
        distance, anchor_label = 0.0, ""

    di_spread = saturating(f.plus_di - f.minus_di, full_scale=25.0) if is_usable(f.plus_di, f.minus_di) else 0.0

    raw = 0.55 * stack + 0.25 * distance + 0.20 * di_spread
    strength = _adx_multiplier(f.adx)
    score = clamp(raw * strength)

    stack_word = "aligned bullish" if stack > 50 else "aligned bearish" if stack < -50 else "tangled"
    adx_text = f"ADX {f.adx:.0f}" if math.isfinite(f.adx) else "ADX n/a"
    detail = f"EMA stack {stack_word}, {adx_text}"
    if anchor_label:
        side = "above" if f.close > anchor else "below"
        detail += f", price {side} {anchor_label}"
    return Driver("Trend", score, weight, detail)


def _adx_multiplier(adx: float) -> float:
    """Trend strength as a confidence multiplier, not a direction."""
    if not math.isfinite(adx):
        return 0.7                      # unknown strength: neither trusted nor discarded
    if adx < ADX_NO_TREND:
        return 0.35
    if adx < ADX_TRENDING:
        return 0.35 + 0.45 * (adx - ADX_NO_TREND) / (ADX_TRENDING - ADX_NO_TREND)
    return min(1.0, 0.8 + (adx - ADX_TRENDING) / 100.0)


def _momentum(f: TechnicalFeatures, weight: float) -> Driver:
    """RSI level, MACD histogram level and slope, stochastic position."""
    parts: list[tuple[float, float]] = []
    notes: list[str] = []

    if is_usable(f.rsi):
        rsi_score, exhausted = _oscillator(f.rsi, RSI_EXHAUSTED_LOW, RSI_EXHAUSTED_HIGH)
        notes.append(f"RSI {f.rsi:.0f}{' (exhausted)' if exhausted else ''}")
        parts.append((rsi_score, 0.35))

    if is_usable(f.macd_hist, f.macd_scale) and f.macd_scale > 0:
        level = saturating(f.macd_hist, full_scale=0.5 * f.macd_scale)
        if is_usable(f.macd_hist_prev):
            slope = saturating(f.macd_hist - f.macd_hist_prev, full_scale=0.2 * f.macd_scale)
            macd_score = clamp(0.7 * level + 0.3 * slope)
            direction = "expanding" if abs(f.macd_hist) > abs(f.macd_hist_prev) else "fading"
        else:
            macd_score, direction = level, "flat"
        notes.append(f"MACD {'+' if f.macd_hist >= 0 else '-'}ve {direction}")
        parts.append((macd_score, 0.40))

    if is_usable(f.stoch_k):
        stoch_score, _ = _oscillator(f.stoch_k, STOCH_EXHAUSTED_LOW, STOCH_EXHAUSTED_HIGH)
        parts.append((stoch_score, 0.25))

    if not parts:
        return Driver("Momentum", 0.0, 0.0, "momentum indicators have not warmed up")
    return Driver("Momentum", weighted(parts), weight, ", ".join(notes) or "momentum mixed")


def _oscillator(value: float, low_extreme: float, high_extreme: float) -> tuple[float, bool]:
    """Score a 0..100 oscillator, decaying once the reading is stretched.

    Returns (score, exhausted). Without the decay a reading of 95 and a reading
    of 75 both clip to full marks, and the card would rate the worst entry in
    the move exactly as highly as the best one.
    """
    base = saturating(value - 50.0, full_scale=40.0)
    if value > high_extreme:
        progress = min(1.0, (value - high_extreme) / max(1e-9, 100.0 - high_extreme))
    elif value < low_extreme:
        progress = min(1.0, (low_extreme - value) / max(1e-9, low_extreme))
    else:
        return base, False
    return base * (1.0 - (1.0 - EXHAUSTION_FLOOR) * progress), True


def _volatility(f: TechnicalFeatures, weight: float) -> Driver:
    """Where price sits in its Bollinger range, scaled by whether the range is expanding.

    Direction alone is weak evidence -- price rides the upper band in a grind
    as well as in a breakout. Expansion is what separates the two, so it acts
    as a multiplier on the directional read rather than a score of its own.
    """
    if not is_usable(f.bb_position):
        return Driver("Volatility", 0.0, 0.0, "Bollinger bands have not warmed up")

    ratios = [r for r in (f.atr_expansion,
                          f.bb_width / f.bb_width_median if is_usable(f.bb_width, f.bb_width_median) and f.bb_width_median > 0 else float("nan"))
              if math.isfinite(r)]
    if ratios:
        expansion = max(ratios)
        # 1.0x is flat, 1.5x is a fully-fledged expansion.
        factor = clamp((expansion - 1.0) / 0.5, 0.0, 1.0)
        state = "expanding" if expansion >= 1.2 else "compressing" if expansion <= 0.9 else "steady"
        expansion_text = f"volatility {state} ({expansion:.2f}x)"
    else:
        factor, expansion_text = 0.0, "volatility baseline unknown"

    direction = clamp(f.bb_position, -1.0, 1.0)
    # Even without expansion, band position is worth a quarter weight.
    score = clamp(direction * 100.0 * (0.25 + 0.75 * factor))
    band = "upper band" if direction > 0.4 else "lower band" if direction < -0.4 else "mid band"
    return Driver("Volatility", score, weight, f"{expansion_text}, price at {band}")


def _volume(f: TechnicalFeatures, weight: float) -> Driver:
    """OBV gives the direction of the flow; relative volume says how much to trust it."""
    if not is_usable(f.obv_slope_norm):
        return Driver("Volume", 0.0, 0.0, "OBV has not warmed up")

    flow = saturating(f.obv_slope_norm, full_scale=0.5)
    if is_usable(f.rvol):
        conviction = clamp(f.rvol / 1.5, 0.35, 1.3)
        rvol_text = f"{f.rvol:.1f}x average volume"
    else:
        conviction, rvol_text = 0.7, "volume baseline unknown"

    score = clamp(flow * conviction)
    flow_word = "accumulation" if flow > 15 else "distribution" if flow < -15 else "flat flow"
    return Driver("Volume", score, weight, f"OBV {flow_word}, {rvol_text}")


def _structure(f: TechnicalFeatures, weight: float) -> Driver:
    """Position against rolling VWAP, plus any fresh break of a pivot level."""
    parts: list[tuple[float, float]] = []
    notes: list[str] = []

    if is_usable(f.vwap_distance_atr):
        parts.append((saturating(f.vwap_distance_atr, full_scale=1.5), 0.55))
        side = "above" if f.vwap_distance_atr > 0 else "below"
        notes.append(f"{abs(f.vwap_distance_atr):.1f} ATR {side} VWAP")

    break_score, break_note = _break_component(f)
    if break_note:
        notes.append(break_note)
    if break_score is not None:
        parts.append((break_score, 0.45))

    if not parts:
        return Driver("Structure", 0.0, 0.0, "no usable structure reference")
    return Driver("Structure", weighted(parts), weight, ", ".join(notes))


def _break_component(f: TechnicalFeatures) -> tuple[float | None, str]:
    if f.broke_resistance and f.broke_support:
        # Both sides taken inside the lookback: a whipsaw, not a break.
        return 0.0, "whipsawing through structure"
    if f.broke_resistance:
        return 85.0, f"broke resistance at {f.resistance:.6g}"
    if f.broke_support:
        return -85.0, f"broke support at {f.support:.6g}"

    if is_usable(f.distance_to_resistance_atr, f.distance_to_support_atr):
        span = f.distance_to_resistance_atr + f.distance_to_support_atr
        if span > 0:
            # Inside a range: room to run below is bullish, overhead supply is not.
            position = (f.distance_to_resistance_atr - f.distance_to_support_atr) / span
            return clamp(position * 30.0), f"ranging, {f.distance_to_resistance_atr:.1f} ATR to resistance"
    return None, ""
