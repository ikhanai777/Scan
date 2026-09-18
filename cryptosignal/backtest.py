"""Phase 4: replay the signal engine over real market history.

Everything here runs on OHLCV pulled from the exchange. There is no simulated
price series, no bootstrap, no synthetic path -- if the venue will not serve
the history, the backtest does not run.

**Three rules keep this from flattering itself.** They are the difference
between a backtest and a sales pitch:

1. **No lookahead.** A signal at bar *t* is computed from bars `0..t` only, and
   is entered at bar `t+1`'s **open** -- the first price actually tradeable
   after the decision. Scoring on a bar's close and entering at that same close
   is the most common way a backtest invents returns that were never available.

2. **The stop is checked before the target, inside every bar.** When a bar's
   range covers both, we cannot know which came first, so the loss is assumed.
   OHLC gives high and low, so this is checked against the real extremes rather
   than against closes.

3. **An expiry is marked to market** at the close of the bar where the window
   ran out, never at the best price the trade ever saw. Peak and trough are
   recorded separately as information.

**What this can and cannot tune.** It replays the technical leg, the screen and
the level logic, so it can tune their weights, the threshold band, the stop
distance and the target multiples. It cannot tune the *leg* weights: that would
need historical funding, order books and headlines aligned to each bar, and
none of those are available free at bar resolution. Tuning technical against
real history and leaving the leg split at the spec's 50/25/25 is the honest
position, and `sweep` will not pretend otherwise.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .config import Settings
from .features import MIN_BARS, compute_features
from .fusion import fuse
from .legs import score_technical
from .levels import build_levels
from .models import Candles, Direction, Levels, MarketSnapshot, timeframe_minutes
from .screen import setup_score

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestTrade:
    """One replayed signal, graded against the bars that followed it."""

    symbol: str
    direction: Direction
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    levels: Levels
    composite: float
    confidence: float
    setup_score: float
    outcome: str                  # target2 | target1 | stop | expired
    realized_r: float
    peak_r: float
    trough_r: float

    @property
    def bars_held(self) -> int:
        return self.exit_index - self.entry_index


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    bars: int
    trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.realized_r > 0)

    @property
    def hit_rate(self) -> float | None:
        return 100.0 * self.wins / self.count if self.count else None

    @property
    def expectancy_r(self) -> float | None:
        return sum(t.realized_r for t in self.trades) / self.count if self.count else None

    @property
    def total_r(self) -> float:
        return sum(t.realized_r for t in self.trades)

    @property
    def max_drawdown_r(self) -> float:
        equity = peak = worst = 0.0
        for trade in self.trades:
            equity += trade.realized_r
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    @property
    def avg_bars_held(self) -> float | None:
        return sum(t.bars_held for t in self.trades) / self.count if self.count else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "trades": self.count,
            "wins": self.wins,
            "losses": self.count - self.wins,
            "hit_rate": round(self.hit_rate, 1) if self.hit_rate is not None else None,
            "expectancy_r": round(self.expectancy_r, 3) if self.expectancy_r is not None else None,
            "total_r": round(self.total_r, 2),
            "max_drawdown_r": round(self.max_drawdown_r, 2),
            "avg_bars_held": round(self.avg_bars_held, 1) if self.avg_bars_held is not None else None,
            "outcomes": {
                name: sum(1 for t in self.trades if t.outcome == name)
                for name in ("target2", "target1", "stop", "expired")
            },
        }


@dataclass
class Portfolio:
    """Several symbols' results, combined the way a trader would experience them."""

    results: list[BacktestResult] = field(default_factory=list)

    @property
    def trades(self) -> list[BacktestTrade]:
        every = [t for r in self.results for t in r.trades]
        return sorted(every, key=lambda t: t.entry_index)

    def to_dict(self) -> dict[str, Any]:
        combined = BacktestResult("portfolio", "", 0, self.trades)
        summary = combined.to_dict()
        summary["symbols"] = len(self.results)
        summary["per_symbol"] = [r.to_dict() for r in self.results]
        return summary


def backtest_candles(candles: Candles, settings: Settings,
                     score_leg: Callable | None = None) -> BacktestResult:
    """Replay one symbol's real history bar by bar.

    `score_leg` exists so a sweep can swap the scoring function; it defaults to
    the shipped technical leg.
    """
    score_leg = score_leg or score_technical
    result = BacktestResult(candles.symbol, candles.timeframe, len(candles))
    if len(candles) < MIN_BARS + 5:
        return result

    minutes_per_bar = timeframe_minutes(candles.timeframe)
    index = MIN_BARS
    # A signal occupies its symbol until it closes, exactly as the live scanner
    # only allows one open signal per coin.
    while index < len(candles) - 2:
        window = _slice(candles, index + 1)
        features = compute_features(window)
        if features is None:
            index += 1
            continue

        snapshot = MarketSnapshot(
            symbol=candles.symbol, base=candles.symbol.split("/")[0],
            quote=candles.symbol.partition("/")[2], last=features.close,
            quote_volume_24h=float("nan"), spread_bps=float("nan"), change_24h_pct=float("nan"),
        )
        candidate = setup_score(snapshot, features, settings)
        fusion = fuse([score_leg(features, settings)], settings)
        if not fusion.fired:
            index += 1
            continue

        try:
            levels = build_levels(features, fusion.direction, settings)
        except ValueError:
            index += 1
            continue

        # Rule 1: enter at the next bar's open, the first tradeable price.
        entry_index = index + 1
        entry_price = candles.open[entry_index]
        trade = _walk_forward(candles, entry_index, entry_price, fusion.direction,
                              levels, minutes_per_bar, candidate.setup_score,
                              fusion.composite, fusion.confidence)
        if trade is None:
            index += 1
            continue

        result.trades.append(trade)
        index = trade.exit_index + 1

    return result


def _walk_forward(candles: Candles, entry_index: int, entry_price: float,
                  direction: Direction, levels: Levels, minutes_per_bar: int,
                  setup: float, composite: float, confidence: float) -> BacktestTrade | None:
    """Grade a trade against the real bars that followed it."""
    risk = abs(entry_price - levels.stop)
    if risk <= 0:
        return None
    sign = direction.sign
    max_bars = max(1, round(levels.hold_minutes / minutes_per_bar))
    last_index = min(len(candles) - 1, entry_index + max_bars)

    peak = trough = 0.0
    target1_hit = False

    for i in range(entry_index, last_index + 1):
        high, low = candles.high[i], candles.low[i]
        best = (high - entry_price) * sign / risk if sign > 0 else (entry_price - low) / risk
        worst = (low - entry_price) * sign / risk if sign > 0 else (entry_price - high) / risk
        peak, trough = max(peak, best), min(trough, worst)

        # Rule 2: the stop is checked first, inside the bar.
        if (sign > 0 and low <= levels.stop) or (sign < 0 and high >= levels.stop):
            return BacktestTrade(candles.symbol, direction, entry_index, i, entry_price,
                                 levels.stop, levels, composite, confidence, setup,
                                 "stop", -1.0, peak, trough)

        if (sign > 0 and high >= levels.target2) or (sign < 0 and low <= levels.target2):
            realized = abs(levels.target2 - entry_price) / risk
            return BacktestTrade(candles.symbol, direction, entry_index, i, entry_price,
                                 levels.target2, levels, composite, confidence, setup,
                                 "target2", realized, peak, trough)

        if not target1_hit and ((sign > 0 and high >= levels.target1)
                                or (sign < 0 and low <= levels.target1)):
            target1_hit = True

    # Rule 3: the window ran out. Mark to the close of the final bar.
    exit_price = candles.close[last_index]
    realized = sign * (exit_price - entry_price) / risk
    outcome = "target1" if target1_hit else "expired"
    return BacktestTrade(candles.symbol, direction, entry_index, last_index, entry_price,
                         exit_price, levels, composite, confidence, setup,
                         outcome, realized, peak, trough)


def _slice(candles: Candles, end: int) -> Candles:
    """Bars `0..end-1`. The whole no-lookahead guarantee rests on this."""
    return Candles(
        symbol=candles.symbol, timeframe=candles.timeframe,
        timestamps=candles.timestamps[:end], open=candles.open[:end],
        high=candles.high[:end], low=candles.low[:end],
        close=candles.close[:end], volume=candles.volume[:end],
    )


def backtest_many(histories: Iterable[Candles], settings: Settings) -> Portfolio:
    portfolio = Portfolio()
    for candles in histories:
        result = backtest_candles(candles, settings)
        portfolio.results.append(result)
        log.info("backtest %s: %d bars -> %d trades, %s expectancy",
                 result.symbol, result.bars, result.count,
                 f"{result.expectancy_r:+.3f}R" if result.expectancy_r is not None else "n/a")
    return portfolio


# ---- tuning ----------------------------------------------------------------


@dataclass(frozen=True)
class SweepRow:
    overrides: dict[str, Any]
    summary: dict[str, Any]

    @property
    def expectancy(self) -> float:
        value = self.summary.get("expectancy_r")
        return float("-inf") if value is None else float(value)

    @property
    def trades(self) -> int:
        return int(self.summary.get("trades", 0))


def sweep(histories: Sequence[Candles], settings: Settings,
          grid: dict[str, Sequence[Any]], min_trades: int = 20,
          max_combinations: int = 400) -> list[SweepRow]:
    """Evaluate a parameter grid over real history, best expectancy first.

    `min_trades` is not decoration. A configuration that fired four times and
    won all four has an expectancy of nothing at all, and ranking it first is
    how a sweep talks you into overfitting. Configurations below the floor are
    still returned, but sorted below every configuration that clears it.
    """
    names = sorted(grid)
    combinations = list(itertools.product(*(grid[name] for name in names)))
    if len(combinations) > max_combinations:
        raise ValueError(
            f"grid has {len(combinations)} combinations, above max_combinations="
            f"{max_combinations}. Narrow the grid rather than raising the cap: "
            f"every extra dimension multiplies the chance of fitting noise."
        )

    rows: list[SweepRow] = []
    for values in combinations:
        overrides = dict(zip(names, values, strict=True))
        try:
            candidate_settings = replace(settings, **overrides)
            candidate_settings.validate()
        except (TypeError, ValueError) as exc:
            log.debug("skipping %s: %s", overrides, exc)
            continue
        summary = backtest_many(histories, candidate_settings).to_dict()
        rows.append(SweepRow(overrides, summary))

    rows.sort(key=lambda row: (row.trades >= min_trades, row.expectancy), reverse=True)
    return rows
