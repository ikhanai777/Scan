"""The fundamental / on-chain leg. Phase 2, ~25% of the fused score.

Four components, each from a real source, each abstaining when it has nothing:

| Component      | Source                      | Key needed |
|----------------|-----------------------------|------------|
| Funding rate   | the venue's perp, via ccxt  | no         |
| Open interest  | the venue's perp, via ccxt  | no         |
| Book pressure  | the venue's order book      | no         |
| TVL trend      | DefiLlama public API        | no         |

**What is deliberately missing.** The spec also lists exchange netflow and
whale wallet activity. Every source for those (Glassnode and its peers) is paid
and keyed; there is no free, keyless equivalent. Rather than approximate them
with something that is not them, those two components are absent, and the leg
renormalises over the four that reported. A leg that quietly substituted a
proxy and called it netflow would be worse than a leg that is one component
short and says so.

**Funding is read contrarian at the extremes**, per the spec's "extreme funding
flags reversal risk". Mild funding says nothing much. Heavy positive funding
means the crowd is levered long and paying to stay there, which is a crowded
trade and the side that gets liquidated first -- so it scores bearish, not
bullish. That sign trips people up, and it is the whole point of the component.
"""

from __future__ import annotations

import math

from ..config import Settings
from ..features import TechnicalFeatures
from ..models import Driver, LegName, LegScore
from ..scoring import clamp, is_usable, saturating, weighted
from ..sources.defillama import TVLReading
from ..sources.derivatives import FUNDING_ELEVATED, FUNDING_EXTREME, DerivativesReading
from ..sources.orderbook import BookReading

#: Funding at three times "extreme" is as loud as this component ever gets.
FUNDING_SATURATION = FUNDING_EXTREME * 3.0
#: An OI move of this fraction in one cycle is a full-strength reading.
OI_SATURATION = 0.05
#: Book imbalance is already -1..+1; this is where it saturates the score.
BOOK_SATURATION = 0.35
#: A week of TVL moving this many percent is a full-strength reading.
TVL_SATURATION = 15.0
#: Fewer than this many reporting components and the leg does not vote at all.
MIN_COMPONENTS = 2


def score_fundamental(
    derivatives: DerivativesReading | None,
    book: BookReading | None,
    tvl: TVLReading | None,
    features: TechnicalFeatures | None,
    settings: Settings,
) -> LegScore:
    components = [
        _funding(derivatives, settings.fund_w_funding),
        _open_interest(derivatives, features, settings.fund_w_open_interest),
        _book_pressure(book, settings.fund_w_book),
        _tvl(tvl, settings.fund_w_tvl),
    ]
    voting = [d for d in components if d.weight > 0]

    if len(voting) < MIN_COMPONENTS:
        reported = ", ".join(d.label for d in voting) or "nothing"
        return LegScore(
            leg=LegName.FUNDAMENTAL, score=0.0, drivers=tuple(components), available=False,
            note=f"only {reported} reported -- not enough on-chain data to vote",
        )

    score = weighted([(d.score, d.weight) for d in voting])
    note = "" if len(voting) == 4 else f"{4 - len(voting)} component(s) had no data"
    return LegScore(leg=LegName.FUNDAMENTAL, score=score, drivers=tuple(components), note=note)


def _funding(reading: DerivativesReading | None, weight: float) -> Driver:
    """Crowded positioning, read against the crowd once it gets expensive."""
    if reading is None or reading.funding_rate is None:
        return Driver("Funding", 0.0, 0.0, "no perpetual listed for this pair")

    rate = reading.funding_rate
    annualised = rate * 3 * 365 * 100          # most venues fund every 8 hours
    magnitude = abs(rate)

    if magnitude < FUNDING_ELEVATED:
        # A real reading that genuinely says nothing. This is not an abstention:
        # the venue answered, and the answer is "positioning is unremarkable".
        return Driver("Funding", 0.0, weight,
                      f"funding {rate * 100:+.4f}% ({annualised:+.0f}% annualised), normal")

    # Beyond "elevated" the crowd is paying to hold, so the score opposes it.
    excess = (magnitude - FUNDING_ELEVATED) / max(1e-12, FUNDING_SATURATION - FUNDING_ELEVATED)
    score = clamp(-math.copysign(min(1.0, excess) * 100.0, rate))
    crowd = "longs paying shorts" if rate > 0 else "shorts paying longs"
    heat = "extreme" if magnitude >= FUNDING_EXTREME else "elevated"
    return Driver("Funding", score, weight,
                  f"{heat} funding {rate * 100:+.4f}% ({annualised:+.0f}% annualised), {crowd}")


def _open_interest(reading: DerivativesReading | None, features: TechnicalFeatures | None,
                   weight: float) -> Driver:
    """New money or unwinding -- which one depends on where price went."""
    if reading is None or reading.open_interest_change is None:
        detail = ("open interest not published for this pair"
                  if reading is None or reading.open_interest is None
                  else "first reading for this pair -- no change to compare against yet")
        return Driver("Open interest", 0.0, 0.0, detail)
    if features is None or not is_usable(features.recent_return_pct):
        return Driver("Open interest", 0.0, 0.0, "no price change to read open interest against")

    oi_change = reading.open_interest_change
    price_change = features.recent_return_pct
    strength = saturating(abs(oi_change), OI_SATURATION)

    if abs(price_change) < 0.05:
        return Driver("Open interest", 0.0, weight,
                      f"open interest {oi_change:+.1%} but price flat -- no read")

    if oi_change > 0:
        # Positions opening in the direction price is already going.
        score = clamp(math.copysign(strength, price_change))
        story = "new longs" if price_change > 0 else "new shorts"
        detail = f"open interest {oi_change:+.1%} with price {price_change:+.1f}% -- {story}"
    else:
        # Positions closing: the move is being unwound, so it reads against the
        # move, and at half strength -- unwinding is weaker evidence than entry.
        score = clamp(-math.copysign(strength * 0.5, price_change))
        story = "longs closing" if price_change > 0 else "shorts covering"
        detail = f"open interest {oi_change:+.1%} with price {price_change:+.1f}% -- {story}"

    return Driver("Open interest", score, weight, detail)


def _book_pressure(book: BookReading | None, weight: float) -> Driver:
    """Resting size near mid: more bid than offer, or the other way."""
    if book is None:
        return Driver("Book pressure", 0.0, 0.0, "order book unavailable or too thin to read")

    score = saturating(book.imbalance, BOOK_SATURATION)
    side = "bid-heavy" if book.imbalance > 0.05 else "offer-heavy" if book.imbalance < -0.05 else "balanced"
    return Driver("Book pressure", score, weight,
                  f"{side} book, {book.imbalance:+.0%} imbalance "
                  f"on {book.total_notional / 1e6:.1f}M resting")


def _tvl(reading: TVLReading | None, weight: float) -> Driver:
    """Capital entering or leaving the protocol behind the token."""
    if reading is None:
        return Driver("TVL", 0.0, 0.0, "not a DeFi protocol, or no unique DefiLlama match")

    weekly, daily = reading.change_7d, reading.change_1d
    parts: list[tuple[float, float]] = []
    if weekly is not None and math.isfinite(weekly):
        parts.append((saturating(weekly, TVL_SATURATION), 0.65))
    if daily is not None and math.isfinite(daily):
        parts.append((saturating(daily, TVL_SATURATION / 3.0), 0.35))
    if not parts:
        return Driver("TVL", 0.0, 0.0, f"{reading.protocol} lists TVL but no change history")

    score = weighted(parts)
    trend = "inflows" if score > 10 else "outflows" if score < -10 else "flat"
    weekly_text = f"{weekly:+.1f}% 7d" if weekly is not None else "7d unavailable"
    return Driver("TVL", score, weight,
                  f"{reading.protocol} ${reading.tvl_usd / 1e6:,.0f}M TVL, {weekly_text} ({trend})")
