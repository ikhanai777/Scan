"""The two-stage funnel.

Stage 1 is cheap and runs over every market: liquidity floor, spread ceiling,
no stablecoins or wrapped tokens, nothing that already has an open signal.
Stage 2 costs one indicator pass per survivor and ranks them by how *live* the
setup is -- volatility expanding, volume anomalous, price at or through a
level. Only the top N earn the deep analysis.

The setup score is deliberately direction-agnostic. "Something is happening
here" is a different question from "which way", and mixing them would let a
strong downtrend crowd out a coiling breakout.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from .config import Settings
from .features import TechnicalFeatures
from .models import Candidate, MarketSnapshot

# Within this many ATR of a pivot, a coin is coiled against the level and
# worth a look even though nothing has broken yet.
COILED_ATR = 0.35


def stage1_universe(
    markets: Iterable[MarketSnapshot],
    settings: Settings,
    excluded_symbols: Iterable[str] = (),
) -> list[MarketSnapshot]:
    """Fast filter: tradeable size, tradeable cost, nothing already in play."""
    blocked = {s.upper() for s in excluded_symbols}
    excluded_bases = set(settings.excluded_bases)

    survivors = []
    for market in markets:
        if market.symbol.upper() in blocked:
            continue
        if market.base.upper() in excluded_bases:
            continue
        if market.quote.upper() != settings.quote_currency:
            continue
        if not math.isfinite(market.quote_volume_24h) or market.quote_volume_24h < settings.min_quote_volume_24h:
            continue
        # A missing spread is treated as a pass: some venues omit bid/ask on the
        # ticker endpoint, and dropping those would silently shrink the universe
        # for a reason that has nothing to do with the coin.
        if math.isfinite(market.spread_bps) and market.spread_bps > settings.max_spread_bps:
            continue
        if not math.isfinite(market.last) or market.last <= 0:
            continue
        survivors.append(market)

    survivors.sort(key=lambda m: m.quote_volume_24h, reverse=True)
    return survivors[: settings.universe_size]


def setup_score(market: MarketSnapshot, features: TechnicalFeatures, settings: Settings) -> Candidate:
    """Stage 2: how live is this setup, regardless of direction. 0..100."""
    parts: list[tuple[str, float, float]] = []      # (name, score, weight)
    reasons: list[str] = []

    expansion = _volatility_expansion(features)
    if expansion is not None:
        parts.append(("volatility", expansion, settings.setup_w_volatility))
        if expansion >= 60:
            reasons.append(f"ATR expanding {features.atr_expansion:.2f}x")

    anomaly = _volume_anomaly(features)
    if anomaly is not None:
        parts.append(("volume", anomaly, settings.setup_w_volume))
        if anomaly >= 60:
            reasons.append(f"volume {features.rvol:.1f}x average")

    action = _price_action(features)
    if action is not None:
        score, why = action
        parts.append(("price_action", score, settings.setup_w_price_action))
        if why:
            reasons.append(why)

    # The catalyst component is the news/sentiment trigger from the spec. It
    # has no source until phase 3, so it abstains and its weight is
    # redistributed rather than scoring every coin a flat zero.
    catalyst = _catalyst(features)
    if catalyst is not None:
        parts.append(("catalyst", catalyst, settings.setup_w_catalyst))

    total_weight = sum(w for _, _, w in parts)
    composite = sum(s * w for _, s, w in parts) / total_weight if total_weight > 0 else 0.0

    return Candidate(
        market=market,
        setup_score=round(composite, 1),
        components={name: round(score, 1) for name, score, _ in parts},
        reasons=tuple(reasons),
    )


def shortlist(candidates: Iterable[Candidate], settings: Settings) -> list[Candidate]:
    ranked = sorted(candidates, key=lambda c: c.setup_score, reverse=True)
    return ranked[: settings.shortlist_size]


def _volatility_expansion(f: TechnicalFeatures) -> float | None:
    """1.0x of its own baseline scores 0; 1.6x scores 100."""
    if not math.isfinite(f.atr_expansion):
        return None
    return float(min(100.0, max(0.0, (f.atr_expansion - 1.0) / 0.6 * 100.0)))


def _volume_anomaly(f: TechnicalFeatures) -> float | None:
    """Average volume scores 0; 2.5x average scores 100."""
    if not math.isfinite(f.rvol):
        return None
    return float(min(100.0, max(0.0, (f.rvol - 1.0) / 1.5 * 100.0)))


def _price_action(f: TechnicalFeatures) -> tuple[float, str] | None:
    """A fresh break scores full marks; coiling against a level scores partial."""
    if f.broke_resistance and not f.broke_support:
        return 100.0, f"broke resistance {f.resistance:.6g}"
    if f.broke_support and not f.broke_resistance:
        return 100.0, f"broke support {f.support:.6g}"
    if f.broke_resistance and f.broke_support:
        return 40.0, "whipsawing through structure"

    distances = [d for d in (f.distance_to_resistance_atr, f.distance_to_support_atr) if math.isfinite(d) and d >= 0]
    if distances:
        nearest = min(distances)
        if nearest <= COILED_ATR:
            return 65.0, f"coiled {nearest:.2f} ATR from a level"
        # Falls off to zero by 2 ATR away from anything.
        return float(max(0.0, 40.0 * (1.0 - nearest / 2.0))), ""

    if math.isfinite(f.vwap_distance_atr):
        return float(min(50.0, abs(f.vwap_distance_atr) * 25.0)), ""
    return None


def _catalyst(f: TechnicalFeatures) -> float | None:
    """Phase 3 wires the news/sentiment trigger in here. Until then: abstain."""
    return None
