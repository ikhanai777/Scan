"""The news / sentiment leg. Phase 3, ~25% of the fused score.

Three components:

| Component      | Source                          | Key needed |
|----------------|---------------------------------|------------|
| News impact    | public RSS (+ CryptoPanic)      | no (yes for CryptoPanic) |
| Mention velocity | the same headlines            | no         |
| Macro regime   | alternative.me Fear & Greed     | no         |

**News impact** classifies each headline against an event lexicon and weights it
by a recency decay -- a listing from twenty minutes ago outweighs one from
yesterday, with a configurable half life. A coin no headline mentions produces
no reading at all; it does not score zero.

**Mention velocity** is coverage against the coin's own baseline in the same
window. A spike is the signal, and its *direction* comes from the impact score,
because a coin in the news five times as often as usual is a large move either
way, and which way is the classifier's job, not the counter's.

**Macro regime** is the one component that is identical for every coin in a
cycle, by design -- it is the spec's filter for setups that fight the broader
market. Read contrarian at the extremes.

The lexicon and these weights are reasoned, not fitted. Phase 4's backtest is
what turns them into something evidenced, and until it runs they should be
treated as a starting point.
"""

from __future__ import annotations

import math

from ..config import Settings
from ..models import Driver, LegName, LegScore
from ..scoring import clamp, saturating, weighted
from ..sources.fear_greed import EXTREME_FEAR, EXTREME_GREED, RegimeReading
from ..sources.news import NewsReading

#: Coverage this many times its own baseline is a full-strength spike.
VELOCITY_SATURATION = 3.0
#: A spike only amplifies; on its own it never exceeds this.
VELOCITY_MAX_ALONE = 35.0
#: Fewer than this many reporting components and the leg does not vote.
MIN_COMPONENTS = 2


def score_sentiment(news: NewsReading | None, regime: RegimeReading | None,
                    settings: Settings) -> LegScore:
    components = [
        _news_impact(news, settings.sent_w_news),
        _velocity(news, settings.sent_w_velocity),
        _regime(regime, settings.sent_w_regime),
    ]
    voting = [d for d in components if d.weight > 0]

    if len(voting) < MIN_COMPONENTS:
        reported = ", ".join(d.label for d in voting) or "nothing"
        return LegScore(
            leg=LegName.SENTIMENT, score=0.0, drivers=tuple(components), available=False,
            note=f"only {reported} reported -- not enough sentiment data to vote",
        )

    score = weighted([(d.score, d.weight) for d in voting])
    note = "" if len(voting) == 3 else f"{3 - len(voting)} component(s) had no data"
    return LegScore(leg=LegName.SENTIMENT, score=score, drivers=tuple(components), note=note)


def _news_impact(reading: NewsReading | None, weight: float) -> Driver:
    if reading is None:
        return Driver("News", 0.0, 0.0, "no headline in the window mentions this coin")

    headline = reading.top_items[0][0].title if reading.top_items else ""
    if abs(reading.impact) < 1.0:
        # Covered, but by nothing the lexicon recognises as an event. That is a
        # real reading -- there is coverage and it is unremarkable.
        return Driver("News", 0.0, weight,
                      f"{reading.mentions_recent} recent mention(s), no classified event")

    tone = "bullish" if reading.impact > 0 else "bearish"
    trimmed = headline if len(headline) <= 70 else headline[:67] + "..."
    return Driver("News", clamp(reading.impact), weight,
                  f"{tone} coverage, decayed impact {reading.impact:+.0f}: \"{trimmed}\"")


def _velocity(reading: NewsReading | None, weight: float) -> Driver:
    """A coverage spike, pointed the way the classifier points."""
    if reading is None:
        return Driver("Mention velocity", 0.0, 0.0, "no coverage to measure a rate against")

    velocity = reading.velocity
    if velocity is None:
        return Driver("Mention velocity", 0.0, 0.0,
                      f"{reading.mentions_recent} recent mention(s) but no baseline to compare")

    excess = saturating(max(0.0, velocity - 1.0), VELOCITY_SATURATION - 1.0)
    if excess < 5.0:
        return Driver("Mention velocity", 0.0, weight,
                      f"coverage at {velocity:.1f}x its baseline -- normal")

    # Direction comes from the impact classifier. With a spike but no classified
    # event, the magnitude is capped: something is happening, unclear what.
    if abs(reading.impact) < 1.0:
        score = 0.0
        detail = f"coverage spike {velocity:.1f}x baseline, but no classified event"
    else:
        score = clamp(math.copysign(min(excess, 100.0), reading.impact))
        if abs(score) > VELOCITY_MAX_ALONE and abs(reading.impact) < 30.0:
            score = math.copysign(VELOCITY_MAX_ALONE, score)
        detail = (f"coverage spike {velocity:.1f}x baseline "
                  f"({reading.mentions_recent} vs {reading.mentions_baseline} normal)")
    return Driver("Mention velocity", score, weight, detail)


def _regime(reading: RegimeReading | None, weight: float) -> Driver:
    """The whole market's positioning, faded at the extremes."""
    if reading is None:
        return Driver("Market regime", 0.0, 0.0, "Fear & Greed index unavailable")

    value = reading.value
    if EXTREME_FEAR < value < EXTREME_GREED:
        drift = f", {reading.change:+.0f} on the day" if reading.change is not None else ""
        return Driver("Market regime", 0.0, weight,
                      f"Fear & Greed {value:.0f} ({reading.label}){drift} -- no extreme to fade")

    if value >= EXTREME_GREED:
        excess = (value - EXTREME_GREED) / (100.0 - EXTREME_GREED)
        score = -clamp(excess * 100.0)         # fade greed
    else:
        excess = (EXTREME_FEAR - value) / EXTREME_FEAR
        score = clamp(excess * 100.0)          # fade fear
    return Driver("Market regime", score, weight,
                  f"Fear & Greed {value:.0f} ({reading.label}) -- fading the crowd")
