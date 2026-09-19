"""Eight open longs on eight alts is one bet, not eight.

This is the risk control that separates people who survive a bad week from
people who do not, and it is the one most signal engines skip entirely.

Alt-coins are not independent instruments. Their returns against USD are
dominated by a single common factor -- call it "crypto beta" -- and in a
sell-off the pairwise correlation of the whole board converges toward 1.0. A
scanner that ranks setups independently and fires the top eight will cheerfully
open eight positions that are the same position, and a portfolio that looks
diversified at 0.5% risk each is really one 4% bet that all resolves together.

So before a signal is allowed to open, this measures its realised correlation
against what is already open, and counts **effective exposure** rather than
position count:

    effective = sum over open positions of max(0, correlation) x their risk

A new long that is 0.9 correlated with three open longs adds far more to that
number than its own 0.5% suggests, and once the total passes the budget, no
further same-direction signals are allowed regardless of how good they look.

Two deliberate choices:

* **Only same-direction exposure accumulates.** A long and a short in
  correlated coins partly hedge; counting them as additive would block the
  one combination that actually reduces risk. Opposite-direction pairs in
  correlated assets are netted, not summed.
* **Negative correlations are floored at zero, not credited.** A -0.2 reading
  between two alts is noise, and treating it as a hedge is how a book ends up
  short "diversification" it never had.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .models import Candles, Direction

#: Bars of overlapping history needed before a correlation means anything.
MIN_OVERLAP = 40
#: Above this, two coins are the same trade for risk purposes.
HIGH_CORRELATION = 0.75


def returns_of(candles: Candles) -> np.ndarray:
    """Simple bar-to-bar returns. Correlating prices instead of returns is the
    classic error: two coins both drifting up have correlated *levels* whatever
    their day-to-day behaviour."""
    close = np.asarray(candles.close, dtype=float)
    if close.size < 2:
        return np.array([])
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.diff(close) / close[:-1]
    return out[np.isfinite(out)]


def correlation(a: Candles, b: Candles, bars: int = 120) -> float | None:
    """Pearson correlation of returns over the overlapping tail. None if thin.

    Returning None rather than 0.0 matters: "we could not measure this" and
    "these are uncorrelated" lead to opposite decisions, and only one of them
    is a reason to open the position.
    """
    ra, rb = returns_of(a), returns_of(b)
    overlap = min(ra.size, rb.size, bars)
    if overlap < MIN_OVERLAP:
        return None
    ra, rb = ra[-overlap:], rb[-overlap:]
    if np.std(ra) == 0 or np.std(rb) == 0:
        return None
    value = float(np.corrcoef(ra, rb)[0, 1])
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class ExposureDecision:
    allowed: bool
    reason: str
    effective_exposure: float          # in units of risk-per-trade
    worst_correlation: float | None
    worst_symbol: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class PortfolioHeat:
    """Tracks what is open and how much of it is really the same bet."""

    #: symbol -> (direction, risk units, its candles)
    open_positions: dict[str, tuple[Direction, float, Candles]] = field(default_factory=dict)

    def add(self, symbol: str, direction: Direction, risk_units: float, candles: Candles) -> None:
        self.open_positions[symbol] = (direction, risk_units, candles)

    def remove(self, symbol: str) -> None:
        self.open_positions.pop(symbol, None)

    @property
    def gross_risk(self) -> float:
        return sum(risk for _, risk, _ in self.open_positions.values())

    def effective_exposure(self, symbol: str, direction: Direction,
                           risk_units: float, candles: Candles,
                           bars: int = 120) -> tuple[float, float | None, str]:
        """Correlation-weighted exposure if this position were added.

        Returns (effective exposure, worst correlation seen, that symbol).
        """
        total = risk_units
        worst: float | None = None
        worst_symbol = ""

        for other_symbol, (other_direction, other_risk, other_candles) in self.open_positions.items():
            if other_symbol == symbol:
                continue
            rho = correlation(candles, other_candles, bars)
            if rho is None:
                # Unmeasurable: assume it is correlated rather than assume it
                # is free. The conservative read is the right default here.
                rho = HIGH_CORRELATION
            same_side = direction is other_direction
            # Opposite sides in correlated assets partly hedge; netting rather
            # than summing is what keeps the one risk-reducing combination
            # from being blocked.
            contribution = max(0.0, rho) * other_risk * (1.0 if same_side else -1.0)
            total += contribution

            if worst is None or rho > worst:
                worst, worst_symbol = rho, other_symbol

        return max(0.0, total), worst, worst_symbol

    def check(self, symbol: str, direction: Direction, risk_units: float,
              candles: Candles, budget: float, max_correlation: float,
              bars: int = 120) -> ExposureDecision:
        """May this position open, given what is already on?"""
        if not self.open_positions:
            return ExposureDecision(True, "first position", risk_units, None)

        exposure, worst, worst_symbol = self.effective_exposure(
            symbol, direction, risk_units, candles, bars)

        if worst is not None and worst >= max_correlation:
            same_side = any(
                direction is other_direction
                for other, (other_direction, _, _) in self.open_positions.items()
                if other == worst_symbol
            )
            if same_side:
                return ExposureDecision(
                    False,
                    f"{worst:.2f} correlated with open {worst_symbol} -- "
                    f"same trade, not a second one",
                    exposure, worst, worst_symbol,
                )

        if exposure > budget:
            return ExposureDecision(
                False,
                f"correlation-weighted exposure {exposure:.2f} would exceed the "
                f"{budget:.2f} budget (gross {self.gross_risk + risk_units:.2f})",
                exposure, worst, worst_symbol,
            )

        return ExposureDecision(True, f"effective exposure {exposure:.2f} of {budget:.2f}",
                                exposure, worst, worst_symbol)

    def snapshot(self, bars: int = 120) -> dict[str, object]:
        """What the dashboard shows: how concentrated the book really is."""
        symbols = list(self.open_positions)
        pairs = []
        for i, first in enumerate(symbols):
            for second in symbols[i + 1:]:
                rho = correlation(self.open_positions[first][2],
                                  self.open_positions[second][2], bars)
                if rho is not None:
                    pairs.append({"pair": f"{first} / {second}", "correlation": round(rho, 2)})
        pairs.sort(key=lambda row: row["correlation"], reverse=True)

        return {
            "open": len(symbols),
            "gross_risk": round(self.gross_risk, 3),
            "most_correlated": pairs[:5],
            # The count of genuinely independent bets, by the standard
            # rule of thumb: n positions at average correlation rho behave
            # like n / (1 + (n-1)rho) independent ones.
            "effective_positions": round(_effective_count(symbols, pairs), 2),
        }


def _effective_count(symbols: list[str], pairs: list[dict]) -> float:
    n = len(symbols)
    if n <= 1 or not pairs:
        return float(n)
    average = sum(max(0.0, row["correlation"]) for row in pairs) / len(pairs)
    return float(n / (1.0 + (n - 1) * average))
