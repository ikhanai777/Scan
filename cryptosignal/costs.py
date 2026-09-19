"""What it actually costs to get in and out.

A backtest without costs is the single most common way a losing strategy looks
profitable. The arithmetic is brutal and worth stating plainly: at a 1.5 ATR
stop, a round trip of 10bps fees + 4bps spread + 5bps slippage on a coin whose
ATR is 0.5% of price costs roughly **0.04R**. That sounds like nothing until
you notice a system doing 200 trades a year at +0.08R expectancy just lost half
its edge, and one at +0.04R lost all of it.

Three components, each modelled separately because they behave differently:

* **Fees** are a known percentage, and differ by whether you took liquidity or
  made it. The entry is a post-only limit order, so it pays the maker fee. The
  exit is a market order, so it pays taker. Getting this backwards flatters the
  result by roughly the maker/taker spread on every trade.

* **Spread** costs you half the quoted bid/ask on any order that crosses. A
  resting limit order does not pay it -- that is what post-only buys you -- so
  it is charged on the exit only.

* **Slippage** is the part everyone waves away. It is charged here on the exit,
  scaled by volatility, because a market order during the move that triggered
  your stop is exactly when the book is thinnest. Modelling it as a flat
  percentage of price would understate it on the volatile coins where stops
  actually get hit.

The defaults are deliberately pessimistic. A backtest that survives costs that
are slightly too high is a better bet than one tuned to costs that are slightly
too low.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Direction


@dataclass(frozen=True)
class CostModel:
    """Round-trip execution cost, in the units the venue charges them."""

    maker_fee_bps: float = 2.0      # resting limit order
    taker_fee_bps: float = 5.0      # crossing market order
    spread_bps: float = 4.0         # full quoted spread; you pay half when crossing
    #: Slippage as a fraction of ATR, charged on the exit. 0.05 means an exit
    #: fills 5% of one ATR worse than the level it triggered at.
    slippage_atr_fraction: float = 0.05
    #: Floor, for a coin whose ATR is tiny relative to its tick.
    min_slippage_bps: float = 1.0

    def entry_price(self, intended: float, direction: Direction) -> float:
        """A post-only limit fill: fee only, no spread, no slippage.

        It either fills at the price you asked for or it does not fill at all,
        which is the whole reason the live broker uses post-only.
        """
        fee = intended * self.maker_fee_bps / 10_000.0
        # A fee makes a long's effective entry worse (higher) and a short's
        # worse (lower) -- "worse" always means against the position.
        return intended + direction.sign * fee

    def exit_price(self, intended: float, direction: Direction, atr: float) -> float:
        """A market exit: taker fee, half the spread, and volatility slippage."""
        fee = intended * self.taker_fee_bps / 10_000.0
        half_spread = intended * (self.spread_bps / 2.0) / 10_000.0

        slip = atr * self.slippage_atr_fraction if math.isfinite(atr) and atr > 0 else 0.0
        slip = max(slip, intended * self.min_slippage_bps / 10_000.0)

        # Every component is charged against the position: a long exits lower
        # than it hoped, a short exits higher.
        return intended - direction.sign * (fee + half_spread + slip)

    def round_trip_bps(self) -> float:
        """Headline cost of a round trip, ignoring slippage. For reporting."""
        return self.maker_fee_bps + self.taker_fee_bps + self.spread_bps / 2.0

    def cost_in_r(self, entry: float, stop: float, atr: float) -> float:
        """The same cost expressed in R, which is the number that matters.

        R is the distance to the stop, so the *same* fee costs far more on a
        tight stop than a wide one. A system whose edge is 0.05R and whose
        costs are 0.04R has no edge, and this is the function that says so.
        """
        risk = abs(entry - stop)
        if risk <= 0 or entry <= 0:
            return 0.0
        fees = entry * (self.maker_fee_bps + self.taker_fee_bps) / 10_000.0
        half_spread = entry * (self.spread_bps / 2.0) / 10_000.0
        slip = max(atr * self.slippage_atr_fraction if math.isfinite(atr) and atr > 0 else 0.0,
                   entry * self.min_slippage_bps / 10_000.0)
        return (fees + half_spread + slip) / risk

    @classmethod
    def from_settings(cls, settings) -> CostModel:
        return cls(
            maker_fee_bps=settings.maker_fee_bps,
            taker_fee_bps=settings.taker_fee_bps,
            spread_bps=settings.assumed_spread_bps,
            slippage_atr_fraction=settings.slippage_atr_fraction,
        )


#: Used when a caller has no configured model. Not free -- there is no such
#: thing as a free round trip, and defaulting to zero would be a lie.
DEFAULT_COSTS = CostModel()
