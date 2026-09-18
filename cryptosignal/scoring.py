"""Shared helpers for turning indicator readings into -100..+100 scores."""

from __future__ import annotations

import math

SCORE_MIN = -100.0
SCORE_MAX = 100.0


def clamp(value: float, low: float = SCORE_MIN, high: float = SCORE_MAX) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(low, min(high, value))


def is_usable(*values: float) -> bool:
    """True when every reading a component needs actually warmed up."""
    return all(math.isfinite(v) for v in values)


def saturating(value: float, full_scale: float) -> float:
    """Map a raw reading onto -100..+100, saturating at +/- `full_scale`.

    `full_scale` is the reading at which the component is as confident as it
    will ever get. Beyond it the score stops growing -- an RSI of 95 is not
    twice the signal an RSI of 85 is.
    """
    if not math.isfinite(value) or full_scale <= 0:
        return 0.0
    return clamp(100.0 * value / full_scale)


def weighted(parts: list[tuple[float, float]]) -> float:
    """Weighted mean of (score, weight) pairs, renormalised over what is present."""
    usable = [(s, w) for s, w in parts if w > 0]
    total_weight = sum(w for _, w in usable)
    if total_weight <= 0:
        return 0.0
    return clamp(sum(s * w for s, w in usable) / total_weight)
