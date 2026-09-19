"""Is this edge real, or did we fit it?

Two questions a backtest cannot answer about itself, and the standard answers.

**Walk-forward.** A sweep that optimises and reports on the same window is not
evidence — it is a description of that window. Walk-forward splits history into
consecutive folds, fits on each in-sample block, and reports results only from
the *next* block, which the optimiser never saw. The out-of-sample curve is the
one that resembles what you would have experienced. A strategy whose in-sample
expectancy is +0.4R and whose out-of-sample is −0.05R has not been improved by
tuning; it has been fitted, and the gap between those two numbers is the size
of the self-deception.

**Significance.** Twelve trades at a 60% hit rate is not a 60% hit rate; it is
seven wins. The bootstrap here resamples the realised trades with replacement
and reports where the middle 90% of expectancies land. If that interval
straddles zero, the honest summary is "we cannot tell this from noise yet",
however good the point estimate looks.

Monte Carlo on the *order* of trades answers a different question that ruins
more accounts than expectancy does: the same set of trades in a different
sequence produces a different worst drawdown, and the one you actually got was
a draw from that distribution. Sizing against the drawdown you happened to see
is sizing against one sample.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .backtest import BacktestTrade, Portfolio, SweepRow, backtest_many, sweep
from .config import Settings
from .models import Candles

log = logging.getLogger(__name__)


# ---- walk-forward ----------------------------------------------------------


@dataclass
class Fold:
    index: int
    train_bars: tuple[int, int]
    test_bars: tuple[int, int]
    chosen: dict[str, Any]
    in_sample: dict[str, Any]
    out_of_sample: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.index,
            "train_bars": list(self.train_bars),
            "test_bars": list(self.test_bars),
            "chosen": self.chosen,
            "in_sample_expectancy_r": self.in_sample.get("expectancy_r"),
            "in_sample_trades": self.in_sample.get("trades"),
            "out_of_sample_expectancy_r": self.out_of_sample.get("expectancy_r"),
            "out_of_sample_trades": self.out_of_sample.get("trades"),
        }


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    out_of_sample_trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def in_sample_expectancy(self) -> float | None:
        values = [f.in_sample.get("expectancy_r") for f in self.folds]
        usable = [v for v in values if v is not None]
        return sum(usable) / len(usable) if usable else None

    @property
    def out_of_sample_expectancy(self) -> float | None:
        if not self.out_of_sample_trades:
            return None
        return sum(t.realized_r for t in self.out_of_sample_trades) / len(self.out_of_sample_trades)

    @property
    def degradation(self) -> float | None:
        """How much of the in-sample edge did not survive. The overfitting bill."""
        inside, outside = self.in_sample_expectancy, self.out_of_sample_expectancy
        if inside is None or outside is None:
            return None
        return inside - outside

    def verdict(self) -> str:
        outside = self.out_of_sample_expectancy
        if outside is None:
            return "no out-of-sample trades -- nothing to judge"
        if len(self.out_of_sample_trades) < 30:
            return (f"only {len(self.out_of_sample_trades)} out-of-sample trades -- "
                    f"too few to conclude anything")
        if outside <= 0:
            return ("the edge does not survive out of sample: this is a fitted "
                    "result, not a strategy")
        gap = self.degradation
        if gap is not None and gap > abs(outside):
            return ("more than half the in-sample edge vanished out of sample -- "
                    "treat the tuning as noise-fitting")
        return "the edge survives out of sample, on this history"

    def to_dict(self) -> dict[str, Any]:
        return {
            "folds": [f.to_dict() for f in self.folds],
            "in_sample_expectancy_r": _round(self.in_sample_expectancy),
            "out_of_sample_expectancy_r": _round(self.out_of_sample_expectancy),
            "out_of_sample_trades": len(self.out_of_sample_trades),
            "degradation_r": _round(self.degradation),
            "verdict": self.verdict(),
        }


def walk_forward(candles: Candles, settings: Settings, grid: dict[str, Sequence[Any]],
                 folds: int = 4, train_ratio: float = 0.7,
                 min_trades: int = 10) -> WalkForwardResult:
    """Roll an optimise-then-test window through real history.

    Each fold fits on its own leading `train_ratio` of the block and is judged
    only on the remainder, which the optimiser never saw.
    """
    result = WalkForwardResult()
    total = len(candles)
    if folds < 1 or total < 300:
        return result

    block = total // folds
    for index in range(folds):
        start = index * block
        stop = total if index == folds - 1 else (index + 1) * block
        split = start + int((stop - start) * train_ratio)
        if split - start < 150 or stop - split < 80:
            continue

        train = _window(candles, start, split)
        test = _window(candles, split, stop)

        rows = sweep([train], settings, grid, min_trades=min_trades)
        if not rows:
            continue
        best = rows[0]
        from dataclasses import replace as dc_replace

        tuned = dc_replace(settings, **best.overrides)
        out = backtest_many([test], tuned).to_dict()
        trades = [t for r in backtest_many([test], tuned).results for t in r.trades]

        result.folds.append(Fold(
            index=index, train_bars=(start, split), test_bars=(split, stop),
            chosen=best.overrides, in_sample=best.summary, out_of_sample=out,
        ))
        result.out_of_sample_trades.extend(trades)
        log.info("fold %d: in-sample %s, out-of-sample %s",
                 index, best.summary.get("expectancy_r"), out.get("expectancy_r"))

    return result


def _window(candles: Candles, start: int, stop: int) -> Candles:
    return Candles(
        symbol=candles.symbol, timeframe=candles.timeframe,
        timestamps=candles.timestamps[start:stop], open=candles.open[start:stop],
        high=candles.high[start:stop], low=candles.low[start:stop],
        close=candles.close[start:stop], volume=candles.volume[start:stop],
    )


# ---- significance ----------------------------------------------------------


@dataclass(frozen=True)
class Significance:
    trades: int
    expectancy: float
    low: float                  # 5th percentile of the bootstrap
    high: float                 # 95th
    probability_positive: float
    median_max_drawdown: float
    worst_max_drawdown: float
    trades_needed: int | None

    @property
    def distinguishable_from_noise(self) -> bool:
        """True only when the whole interval sits on one side of zero."""
        return self.low > 0 or self.high < 0

    def verdict(self) -> str:
        if self.trades < 30:
            return f"{self.trades} trades is too few to conclude anything"
        if not self.distinguishable_from_noise:
            needed = f" (~{self.trades_needed} trades would be needed)" if self.trades_needed else ""
            return f"cannot be distinguished from noise{needed}"
        direction = "positive" if self.low > 0 else "negative"
        return f"expectancy is {direction} with 90% confidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "expectancy_r": round(self.expectancy, 3),
            "confidence_90": [round(self.low, 3), round(self.high, 3)],
            "probability_positive": round(self.probability_positive, 3),
            "median_max_drawdown_r": round(self.median_max_drawdown, 2),
            "worst_max_drawdown_r": round(self.worst_max_drawdown, 2),
            "trades_needed": self.trades_needed,
            "distinguishable_from_noise": self.distinguishable_from_noise,
            "verdict": self.verdict(),
        }


def significance(trades: Sequence[BacktestTrade] | Sequence[float],
                 iterations: int = 2000, seed: int = 11) -> Significance | None:
    """Bootstrap the expectancy and Monte Carlo the drawdown."""
    values = np.array([
        t if isinstance(t, (int, float)) else t.realized_r for t in trades
    ], dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return None

    rng = np.random.default_rng(seed)
    # Resample the trades themselves: the question is "what else could this
    # sample of trades have produced", not "what does a normal curve say".
    draws = rng.choice(values, size=(iterations, values.size), replace=True)
    means = draws.mean(axis=1)

    low, high = float(np.percentile(means, 5)), float(np.percentile(means, 95))
    probability_positive = float((means > 0).mean())

    # Monte Carlo on order: the same trades shuffled produce different worst
    # drawdowns, and the one you saw was a single draw from this distribution.
    drawdowns = np.empty(iterations)
    for i in range(iterations):
        shuffled = rng.permutation(values)
        equity = np.cumsum(shuffled)
        peaks = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
        drawdowns[i] = float(np.max(peaks - equity))

    expectancy = float(values.mean())
    spread = float(values.std(ddof=1)) if values.size > 1 else 0.0
    needed = _trades_needed(expectancy, spread)

    return Significance(
        trades=int(values.size), expectancy=expectancy, low=low, high=high,
        probability_positive=probability_positive,
        median_max_drawdown=float(np.median(drawdowns)),
        worst_max_drawdown=float(np.percentile(drawdowns, 95)),
        trades_needed=needed,
    )


def _trades_needed(expectancy: float, spread: float, z: float = 1.645) -> int | None:
    """How many trades before this expectancy could clear zero at 90%.

    The standard sample-size relation: the standard error falls as 1/sqrt(n),
    so the count needed scales with the square of the noise-to-signal ratio.
    It is a rough number, and its job is to make "not yet" concrete.
    """
    if expectancy == 0 or spread <= 0 or not math.isfinite(expectancy):
        return None
    needed = (z * spread / abs(expectancy)) ** 2
    if not math.isfinite(needed) or needed <= 0:
        return None
    return int(min(100_000, math.ceil(needed)))


def summarise(portfolio: Portfolio, iterations: int = 2000) -> dict[str, Any]:
    """A backtest summary with the statistics attached."""
    summary = portfolio.to_dict()
    stats = significance(portfolio.trades, iterations=iterations)
    summary["significance"] = stats.to_dict() if stats else None
    return summary


def _round(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(value, places)


__all__ = [
    "walk_forward", "WalkForwardResult", "Fold",
    "significance", "Significance", "summarise", "SweepRow",
]
