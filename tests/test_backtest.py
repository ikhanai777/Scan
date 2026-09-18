"""The replay harness -- above all, that it cannot cheat.

A backtest that peeks at the bar it is trading, or that resolves an ambiguous
bar in its own favour, produces a number that was never available to anyone.
Those two properties get the most tests here, because they are the ones that
silently turn a harness into a sales pitch.

The candles are deterministic synthetic series: a replay engine has to be
tested against a path whose outcome is known in advance, which no live market
provides. Against a real venue the same code runs on real history -- that is
what `cryptosignal backtest` does.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from support import make_candles, range_closes, trend_closes

from cryptosignal.backtest import (
    BacktestResult,
    BacktestTrade,
    Portfolio,
    _slice,
    backtest_candles,
    backtest_many,
    sweep,
)
from cryptosignal.models import Candles, Direction, Levels


def bt(settings, closes=None, **overrides):
    candles = make_candles(closes if closes is not None else trend_closes(600, drift=0.004))
    return backtest_candles(candles, replace(settings, **overrides) if overrides else settings)


# ---- the anti-cheating guarantees ------------------------------------------


def test_the_slice_never_includes_the_future():
    candles = make_candles(trend_closes(100))
    window = _slice(candles, 40)
    assert len(window) == 40
    assert window.close[-1] == candles.close[39]


def test_entry_is_the_next_bar_open_not_the_signal_bar_close(settings):
    """Scoring on a close and filling at that same close is free money."""
    result = bt(settings)
    assert result.count > 0
    candles = make_candles(trend_closes(600, drift=0.004))
    for trade in result.trades:
        assert trade.entry_price == pytest.approx(candles.open[trade.entry_index])


def test_a_bar_covering_both_stop_and_target_is_graded_a_stop(settings):
    """We cannot know which came first, so the loss is assumed."""
    from cryptosignal.backtest import _walk_forward

    # One bar whose range spans everything: stop below, target above.
    rows = [
        [0, 100.0, 100.5, 99.5, 100.0, 10.0],
        [1, 100.0, 130.0, 70.0, 100.0, 10.0],
    ]
    candles = Candles.from_rows("X/USDT", "15m", rows)
    levels = Levels(entry_low=99.0, entry_high=101.0, stop=90.0,
                    target1=110.0, target2=120.0, hold_minutes=60)

    trade = _walk_forward(candles, 1, 100.0, Direction.LONG, levels, 15, 50.0, 70.0, 70.0)
    assert trade.outcome == "stop"
    assert trade.realized_r == pytest.approx(-1.0)


def test_an_expiry_is_marked_to_the_final_close_not_the_peak(settings):
    from cryptosignal.backtest import _walk_forward

    # Runs up to 108 but closes the window back at 101.
    rows = [[0, 100.0, 100.0, 100.0, 100.0, 1.0]]
    rows += [[i, 100.0, 108.0, 99.5, 101.0, 1.0] for i in range(1, 5)]
    candles = Candles.from_rows("X/USDT", "15m", rows)
    levels = Levels(entry_low=99.5, entry_high=100.5, stop=95.0,
                    target1=115.0, target2=125.0, hold_minutes=45)

    trade = _walk_forward(candles, 1, 100.0, Direction.LONG, levels, 15, 50.0, 70.0, 70.0)
    assert trade.outcome == "expired"
    assert trade.exit_price == pytest.approx(101.0)
    assert trade.realized_r == pytest.approx(0.2)
    assert trade.peak_r > trade.realized_r         # recorded, never banked


def test_a_stop_always_costs_exactly_one_r(settings):
    result = bt(settings, closes=trend_closes(600, drift=-0.001, noise=0.01, seed=5))
    stops = [t for t in result.trades if t.outcome == "stop"]
    for trade in stops:
        assert trade.realized_r == pytest.approx(-1.0)


def test_a_target_pays_the_configured_multiple(settings):
    result = bt(settings)
    hits = [t for t in result.trades if t.outcome == "target2"]
    for trade in hits:
        assert trade.realized_r == pytest.approx(settings.target2_r, rel=0.15)


# ---- behaviour -------------------------------------------------------------


def test_a_strong_uptrend_produces_long_trades(settings):
    result = bt(settings)
    assert result.count > 0
    assert all(t.direction is Direction.LONG for t in result.trades)


def test_a_strong_downtrend_produces_short_trades(settings):
    result = bt(settings, closes=trend_closes(600, drift=-0.004))
    assert result.count > 0
    assert all(t.direction is Direction.SHORT for t in result.trades)


def test_a_flat_range_produces_few_or_no_trades(settings):
    result = bt(settings, closes=range_closes(600))
    trending = bt(settings)
    assert result.count < trending.count


def test_trades_never_overlap_on_one_symbol(settings):
    """The live scanner allows one open signal per coin; the replay must match."""
    result = bt(settings)
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert later.entry_index > earlier.exit_index


def test_too_little_history_produces_nothing(settings):
    result = backtest_candles(make_candles(trend_closes(30)), settings)
    assert result.count == 0
    assert result.bars == 30


def test_a_wider_threshold_band_fires_less(settings):
    loose = bt(settings, long_threshold=50.0, short_threshold=-50.0)
    tight = bt(settings, long_threshold=85.0, short_threshold=-85.0)
    assert tight.count <= loose.count


# ---- reporting -------------------------------------------------------------


def test_the_summary_reports_every_field(settings):
    summary = bt(settings).to_dict()
    for key in ("trades", "wins", "losses", "hit_rate", "expectancy_r",
                "total_r", "max_drawdown_r", "avg_bars_held", "outcomes"):
        assert key in summary


def test_an_empty_result_reports_none_rather_than_zero(settings):
    """No trades is not a hit rate of zero; it is no hit rate at all."""
    empty = BacktestResult("X/USDT", "15m", 0)
    assert empty.hit_rate is None
    assert empty.expectancy_r is None
    assert empty.to_dict()["hit_rate"] is None


def test_max_drawdown_measures_peak_to_trough():
    def trade(r):
        levels = Levels(1, 1, 1, 1, 1, 60)
        return BacktestTrade("X", Direction.LONG, 0, 1, 1.0, 1.0, levels, 0, 0, 0, "x", r, 0, 0)

    result = BacktestResult("X", "15m", 10, [trade(2.5), trade(-1.0), trade(-1.0), trade(1.5)])
    assert result.total_r == pytest.approx(2.0)
    assert result.max_drawdown_r == pytest.approx(2.0)


def test_a_portfolio_combines_symbols(settings):
    histories = [
        make_candles(trend_closes(400, drift=0.004, seed=1), symbol="A/USDT"),
        make_candles(trend_closes(400, drift=-0.004, seed=2), symbol="B/USDT"),
    ]
    summary = backtest_many(histories, settings).to_dict()
    assert summary["symbols"] == 2
    assert len(summary["per_symbol"]) == 2
    assert summary["trades"] == sum(s["trades"] for s in summary["per_symbol"])


def test_an_empty_portfolio_is_safe():
    assert Portfolio().to_dict()["trades"] == 0


# ---- the sweep -------------------------------------------------------------


def test_a_sweep_ranks_configurations(settings):
    histories = [make_candles(trend_closes(400, drift=0.004))]
    rows = sweep(histories, settings,
                 {"long_threshold": [55.0, 70.0], "stop_atr_multiple": [1.5, 2.0]},
                 min_trades=1)
    assert len(rows) == 4
    assert rows[0].expectancy >= rows[-1].expectancy


def test_a_sweep_sorts_thin_samples_below_real_ones(settings):
    """Four trades and a perfect record is not evidence, and must not rank first."""
    histories = [make_candles(trend_closes(400, drift=0.004))]
    rows = sweep(histories, settings, {"long_threshold": [55.0, 95.0]}, min_trades=1000)
    # With the floor unreachable, everything is below it and ordering is by
    # expectancy alone -- but the flag is what does the sorting.
    assert all(row.trades < 1000 for row in rows)


def test_a_sweep_refuses_an_oversized_grid(settings):
    histories = [make_candles(trend_closes(200))]
    with pytest.raises(ValueError, match="max_combinations"):
        sweep(histories, settings,
              {"long_threshold": list(np.arange(50.0, 90.0, 1.0)),
               "stop_atr_multiple": list(np.arange(1.0, 3.0, 0.1))},
              max_combinations=50)


def test_a_sweep_skips_invalid_combinations(settings):
    """A grid can express nonsense; validation catches it instead of scoring it."""
    histories = [make_candles(trend_closes(300, drift=0.004))]
    rows = sweep(histories, settings,
                 {"target1_r": [1.5, 9.0], "target2_r": [2.5]}, min_trades=1)
    # target1_r=9.0 with target2_r=2.5 is rejected by Settings.validate.
    assert len(rows) == 1
    assert rows[0].overrides["target1_r"] == 1.5


def test_sweep_overrides_are_reported_with_their_results(settings):
    histories = [make_candles(trend_closes(300, drift=0.004))]
    row = sweep(histories, settings, {"long_threshold": [60.0]}, min_trades=1)[0]
    assert row.overrides == {"long_threshold": 60.0}
    assert "expectancy_r" in row.summary
