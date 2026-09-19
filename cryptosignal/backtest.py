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
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .costs import CostModel
from .features import MIN_BARS, compute_features
from .fusion import fuse
from .legs import score_technical
from .levels import build_levels
from .models import Candles, Direction, Levels, MarketSnapshot, timeframe_minutes
from .screen import setup_score

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fill:
    """One execution, with the clock time and the price it happened at.

    A trade is a sequence of these, not a pair of numbers: a partial exit at
    target 1 and the remainder stopping out at breakeven is two fills at two
    different times and prices, and collapsing them to a single "exit" loses
    exactly the detail you need to audit the trade afterwards.
    """

    kind: str                # entry | target1 | target2 | stop | breakeven | expiry
    at: datetime             # wall clock, from the candle's own timestamp
    price: float             # after costs -- what the fill was actually worth
    fraction: float          # share of the original position this fill moved
    r: float | None = None   # realised R on that share; None for the entry

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "price": self.price,
            "fraction": round(self.fraction, 4),
            "r": round(self.r, 3) if self.r is not None else None,
        }


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
    outcome: str                  # target2 | target1 | stop | breakeven | expired
    realized_r: float             # net of costs -- the number that matters
    peak_r: float
    trough_r: float
    entry_at: datetime
    exit_at: datetime
    fills: tuple[Fill, ...] = ()
    gross_r: float = 0.0          # before costs, for measuring what costs took
    cost_r: float = 0.0

    @property
    def bars_held(self) -> int:
        return self.exit_index - self.entry_index

    @property
    def minutes_held(self) -> float:
        return (self.exit_at - self.entry_at).total_seconds() / 60.0

    @property
    def target1_at(self) -> datetime | None:
        return next((f.at for f in self.fills if f.kind == "target1"), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "outcome": self.outcome,
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "minutes_held": round(self.minutes_held, 1),
            "realized_r": round(self.realized_r, 3),
            "gross_r": round(self.gross_r, 3),
            "cost_r": round(self.cost_r, 3),
            "peak_r": round(self.peak_r, 2),
            "trough_r": round(self.trough_r, 2),
            "confidence": round(self.confidence, 1),
            "fills": [f.to_dict() for f in self.fills],
        }


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

    @property
    def avg_minutes_held(self) -> float | None:
        return sum(t.minutes_held for t in self.trades) / self.count if self.count else None

    @property
    def gross_expectancy_r(self) -> float | None:
        """Expectancy before costs. The gap to `expectancy_r` is what costs took."""
        return sum(t.gross_r for t in self.trades) / self.count if self.count else None

    @property
    def cost_per_trade_r(self) -> float | None:
        return sum(t.cost_r for t in self.trades) / self.count if self.count else None

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
            "avg_minutes_held": round(self.avg_minutes_held, 1) if self.avg_minutes_held is not None else None,
            "gross_expectancy_r": round(self.gross_expectancy_r, 3) if self.gross_expectancy_r is not None else None,
            "cost_per_trade_r": round(self.cost_per_trade_r, 3) if self.cost_per_trade_r is not None else None,
            "first_trade_at": self.trades[0].entry_at.isoformat() if self.trades else None,
            "last_trade_at": self.trades[-1].exit_at.isoformat() if self.trades else None,
            "outcomes": {
                name: sum(1 for t in self.trades if t.outcome == name)
                for name in ("target2", "target1", "stop", "breakeven", "expired")
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
    costs = CostModel.from_settings(settings)
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
                              fusion.composite, fusion.confidence,
                              costs, features.atr, settings)
        if trade is None:
            index += 1
            continue

        result.trades.append(trade)
        index = trade.exit_index + 1

    return result


def _walk_forward(candles: Candles, entry_index: int, entry_price: float,
                  direction: Direction, levels: Levels, minutes_per_bar: int,
                  setup: float, composite: float, confidence: float,
                  costs: CostModel, atr: float, settings: Settings) -> BacktestTrade | None:
    """Grade a trade against the real bars that followed it, fill by fill.

    Two things happen here that a naive replay skips, and both change the
    expectancy materially:

    * **Costs are charged on every fill.** The entry pays the maker fee; each
      exit pays taker plus half the spread plus volatility slippage.
    * **Target 1 is an exit, not a milestone.** A configurable fraction comes
      off there and the stop moves to breakeven, which is what the level is
      for. Recording T1 and then letting the whole position ride back to the
      original stop measures a strategy nobody trades.
    """
    sign = direction.sign
    entry_fill = costs.entry_price(entry_price, direction)
    # Risk is measured from the price actually paid, not the price hoped for.
    risk = abs(entry_fill - levels.stop)
    if risk <= 0:
        return None

    at = _timestamp(candles, entry_index)
    fills = [Fill("entry", at, entry_fill, 1.0, None)]

    max_bars = max(1, round(levels.hold_minutes / minutes_per_bar))
    last_index = min(len(candles) - 1, entry_index + max_bars)

    stop = levels.stop
    remaining = 1.0
    realized = gross = 0.0
    peak = trough = 0.0
    target1_done = False
    # Whether the stop has actually been *moved*, which is not the same as
    # whether target 1 was hit: with the breakeven move disabled the original
    # stop still stands, and calling that exit "breakeven" would misreport it.
    stop_moved = False
    partial = max(0.0, min(1.0, settings.partial_exit_fraction))

    def book(kind: str, index: int, level: float, share: float) -> tuple[float, float]:
        """Charge the exit, record the fill, and return (net_r, gross_r)."""
        price = costs.exit_price(level, direction, atr)
        net = sign * (price - entry_fill) / risk
        raw = sign * (level - entry_price) / risk
        fills.append(Fill(kind, _timestamp(candles, index), price, share, net))
        return net, raw

    for i in range(entry_index, last_index + 1):
        high, low = candles.high[i], candles.low[i]
        best = sign * ((high if sign > 0 else low) - entry_fill) / risk
        worst = sign * ((low if sign > 0 else high) - entry_fill) / risk
        peak, trough = max(peak, best), min(trough, worst)

        # Rule 2: the stop is checked first, inside the bar.
        if (sign > 0 and low <= stop) or (sign < 0 and high >= stop):
            kind = "breakeven" if stop_moved else "stop"
            net, raw = book(kind, i, stop, remaining)
            realized += remaining * net
            gross += remaining * raw
            return _finish(candles, entry_index, i, entry_fill, stop, direction, levels,
                           composite, confidence, setup, kind, realized, gross,
                           peak, trough, fills)

        # Target 1: take the partial and move the stop to breakeven.
        if not target1_done and ((sign > 0 and high >= levels.target1)
                                 or (sign < 0 and low <= levels.target1)):
            target1_done = True
            if partial > 0:
                net, raw = book("target1", i, levels.target1, partial)
                realized += partial * net
                gross += partial * raw
                remaining -= partial
            if settings.breakeven_after_target1:
                stop = entry_fill
                stop_moved = True
            if remaining <= 1e-9:
                return _finish(candles, entry_index, i, entry_fill, levels.target1, direction,
                               levels, composite, confidence, setup, "target1",
                               realized, gross, peak, trough, fills)

        if (sign > 0 and high >= levels.target2) or (sign < 0 and low <= levels.target2):
            net, raw = book("target2", i, levels.target2, remaining)
            realized += remaining * net
            gross += remaining * raw
            return _finish(candles, entry_index, i, entry_fill, levels.target2, direction,
                           levels, composite, confidence, setup, "target2",
                           realized, gross, peak, trough, fills)

    # Rule 3: the window ran out. Mark the remainder to the final close.
    exit_level = candles.close[last_index]
    net, raw = book("expiry", last_index, exit_level, remaining)
    realized += remaining * net
    gross += remaining * raw
    outcome = "target1" if target1_done else "expired"
    return _finish(candles, entry_index, last_index, entry_fill, exit_level, direction,
                   levels, composite, confidence, setup, outcome,
                   realized, gross, peak, trough, fills)


def _finish(candles: Candles, entry_index: int, exit_index: int, entry_fill: float,
            exit_level: float, direction: Direction, levels: Levels, composite: float,
            confidence: float, setup: float, outcome: str, realized: float, gross: float,
            peak: float, trough: float, fills: list[Fill]) -> BacktestTrade:
    return BacktestTrade(
        symbol=candles.symbol, direction=direction,
        entry_index=entry_index, exit_index=exit_index,
        entry_price=entry_fill, exit_price=fills[-1].price,
        levels=levels, composite=composite, confidence=confidence, setup_score=setup,
        outcome=outcome, realized_r=realized, peak_r=peak, trough_r=trough,
        entry_at=_timestamp(candles, entry_index), exit_at=_timestamp(candles, exit_index),
        fills=tuple(fills), gross_r=gross, cost_r=gross - realized,
    )


def _timestamp(candles: Candles, index: int) -> datetime:
    """The candle's own open time, as a real UTC datetime."""
    return datetime.fromtimestamp(candles.timestamps[index] / 1000.0, tz=UTC)


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
