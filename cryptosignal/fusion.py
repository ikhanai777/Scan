"""Turn the analysis legs into one call.

Weights come from the spec -- technical ~50%, fundamental ~25%, sentiment ~25%
-- but only legs that actually reported get a vote, and the weights are
renormalised over those. That is what lets phase 1 run technical-only at full
strength without pretending the other two legs said "neutral": a leg with no
data is silent, not neutral, and the two are very different things.

When legs disagree the signal still fires, flagged rather than suppressed, per
the spec. Suppressing disagreement would hide exactly the cases most worth
looking at.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Direction, Driver, LegName, LegScore
from .regime import HigherTimeframe, Regime, RegimeReading, apply_confluence

# Two legs pointing opposite ways by at least this much is a real conflict, not
# noise around zero.
DISAGREEMENT_GAP = 30.0


@dataclass(frozen=True)
class Fusion:
    composite: float
    direction: Direction | None
    confidence: float
    reduced_confidence: bool
    reduced_confidence_reason: str
    leg_scores: dict[str, float]
    drivers: tuple[Driver, ...]
    reporting_legs: tuple[str, ...]
    #: What the higher timeframe and the regime did to this call, for the card.
    raw_composite: float = 0.0
    htf_note: str = ""
    regime: str = ""
    suppressed_reason: str = ""

    @property
    def fired(self) -> bool:
        return self.direction is not None


def fuse(legs: list[LegScore], settings: Settings,
         higher: HigherTimeframe | None = None,
         regime: RegimeReading | None = None) -> Fusion:
    weights = {
        LegName.TECHNICAL: settings.weight_technical,
        LegName.FUNDAMENTAL: settings.weight_fundamental,
        LegName.SENTIMENT: settings.weight_sentiment,
    }

    reporting = [leg for leg in legs if leg.available]
    leg_scores = {leg.leg.value: leg.score for leg in reporting}
    total_weight = sum(weights[leg.leg] for leg in reporting)

    if not reporting or total_weight <= 0:
        return Fusion(
            composite=0.0, direction=None, confidence=0.0,
            reduced_confidence=True, reduced_confidence_reason="no analysis leg reported",
            leg_scores=leg_scores, drivers=(), reporting_legs=(),
        )

    composite = sum(leg.score * weights[leg.leg] for leg in reporting) / total_weight
    raw_composite = composite

    # The two filters a desk applies before looking at a setup at all.
    htf_note = suppressed = ""
    if settings.enable_htf_confluence and higher is not None:
        composite, htf_note = apply_confluence(composite, higher, settings)
        if composite == 0.0 and htf_note:
            suppressed = htf_note

    regime_name = regime.regime.value if regime is not None else ""
    if (settings.skip_choppy_regime and regime is not None
            and regime.regime is Regime.CHOPPY):
        # Chop is where an indicator system bleeds: every level is a fake and
        # every break reverses. Sitting it out is usually free.
        composite = 0.0
        suppressed = f"skipped: {regime.detail}"

    direction: Direction | None = None
    if composite >= settings.long_threshold:
        direction = Direction.LONG
    elif composite <= settings.short_threshold:
        direction = Direction.SHORT

    reduced, reason = _disagreement(reporting, composite, settings)
    confidence = _confidence(composite, direction, settings)
    if reduced and direction is not None:
        confidence *= settings.disagreement_penalty

    return Fusion(
        composite=composite,
        raw_composite=raw_composite,
        htf_note=htf_note,
        regime=regime_name,
        suppressed_reason=suppressed,
        direction=direction,
        confidence=round(confidence, 1),
        reduced_confidence=reduced,
        reduced_confidence_reason=reason,
        leg_scores=leg_scores,
        drivers=_merged_drivers(reporting),
        reporting_legs=tuple(leg.leg.value for leg in reporting),
    )


def _disagreement(reporting: list[LegScore], composite: float, settings: Settings) -> tuple[bool, str]:
    """Flag a call whose legs point opposite ways, or that rests on one leg.

    Running on a single leg is not a disagreement, but it is less evidence than
    the spec's three-leg design assumes, so the card should say so.
    """
    conflicts = []
    for leg in reporting:
        for other in reporting:
            if leg is other:
                continue
            if leg.score >= DISAGREEMENT_GAP and other.score <= -DISAGREEMENT_GAP:
                conflicts.append(f"{leg.leg.value} bullish ({leg.score:+.0f}) vs {other.leg.value} bearish ({other.score:+.0f})")
    if conflicts:
        return True, "; ".join(sorted(set(conflicts)))

    if len(reporting) == 1:
        only = reporting[0].leg.value
        return True, f"{only} leg only -- the other legs had no data"

    # A composite that only just cleared the band is thin on its own.
    threshold = settings.long_threshold if composite > 0 else abs(settings.short_threshold)
    if abs(composite) < threshold + 5.0:
        return True, "composite is only marginally past the threshold"
    return False, ""


def _confidence(composite: float, direction: Direction | None, settings: Settings) -> float:
    """Map |composite| onto the confidence band, linear from threshold to 100."""
    if direction is None:
        return 0.0
    threshold = settings.long_threshold if direction is Direction.LONG else abs(settings.short_threshold)
    span = 100.0 - threshold
    if span <= 0:
        return settings.confidence_ceiling
    progress = min(1.0, max(0.0, (abs(composite) - threshold) / span))
    return settings.confidence_floor + progress * (settings.confidence_ceiling - settings.confidence_floor)


def _merged_drivers(reporting: list[LegScore], count: int = 3) -> tuple[Driver, ...]:
    """The top factors across every leg, ranked by how much each moved its leg."""
    everything: list[Driver] = []
    for leg in reporting:
        everything.extend(d for d in leg.drivers if d.weight > 0)
    ranked = sorted(everything, key=lambda d: abs(d.contribution), reverse=True)
    return tuple(ranked[:count])
