"""Phase 5: the limits between a signal and an order.

Almost every test here asserts a *refusal*. That is the shape the module is
meant to have: default deny, and every allowance explicit and bounded. The one
thing worse than a trading bot that will not trade is one that trades when it
was told not to.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from test_store_and_tracker import make_signal

from cryptosignal.execution import ExecutionEngine, build_engine
from cryptosignal.execution.broker import LiveBroker, PaperBroker
from cryptosignal.execution.risk import (
    LIVE_CONFIRMATION_PHRASE,
    ExecutionMode,
    RiskManager,
    RiskState,
)
from cryptosignal.models import Direction, utcnow
from cryptosignal.tracker import TrackerUpdate

EQUITY = 10_000.0


def paper(settings, **overrides):
    configured = replace(settings, execution_mode="paper", **overrides)
    return ExecutionEngine(configured, PaperBroker(), RiskManager(configured))


# ---- the mode gate ---------------------------------------------------------


def test_execution_is_off_by_default(settings):
    assert RiskManager(settings).mode == ExecutionMode.DISABLED
    assert build_engine(settings) is None


def test_a_disabled_engine_refuses_every_signal(settings):
    engine = ExecutionEngine(settings, PaperBroker(), RiskManager(settings))
    assert engine.on_signal(make_signal(), EQUITY) is None
    assert not engine.enabled


def test_live_without_the_confirmation_phrase_refuses_to_start(settings):
    with pytest.raises(RuntimeError, match="CS_LIVE_CONFIRM"):
        build_engine(replace(settings, execution_mode="live"))


def test_live_with_a_near_miss_phrase_still_refuses(settings):
    almost = replace(settings, execution_mode="live",
                     live_confirm="i understand this places real orders")
    with pytest.raises(RuntimeError, match="CS_LIVE_CONFIRM"):
        build_engine(almost)


def test_live_without_api_keys_refuses_to_start(settings):
    confirmed = replace(settings, execution_mode="live", live_confirm=LIVE_CONFIRMATION_PHRASE)
    with pytest.raises(RuntimeError, match="API_KEY"):
        build_engine(confirmed)


def test_a_live_configuration_never_falls_back_to_paper_silently(settings):
    """Paper-trading someone who believes they are live is its own failure."""
    unconfirmed = replace(settings, execution_mode="live")
    assert RiskManager(unconfirmed).mode == ExecutionMode.LIVE
    assert not RiskManager(unconfirmed).live_confirmed
    decision = RiskManager(unconfirmed).check(make_signal(), EQUITY)
    assert not decision
    assert "not confirmed" in decision.reason


def test_an_unknown_mode_reads_as_disabled(settings):
    assert RiskManager(replace(settings, execution_mode="yolo")).mode == ExecutionMode.DISABLED


def test_paper_mode_builds_a_paper_broker(settings):
    engine = build_engine(replace(settings, execution_mode="paper"))
    assert engine.broker.name == "paper"
    assert engine.enabled


# ---- the limits ------------------------------------------------------------


def test_a_signal_below_the_confidence_floor_is_refused(settings):
    engine = paper(settings, min_execution_confidence=90.0)
    signal = make_signal()
    signal.confidence = 70.0
    assert engine.on_signal(signal, EQUITY) is None
    assert "below the execution floor" in engine.decisions[-1]["reason"]


def test_a_reduced_confidence_signal_is_refused_by_default(settings):
    engine = paper(settings, min_execution_confidence=0.0)
    signal = make_signal()
    signal.reduced_confidence = True
    assert engine.on_signal(signal, EQUITY) is None
    assert "reduced confidence" in engine.decisions[-1]["reason"]


def test_reduced_confidence_can_be_opted_into(settings):
    engine = paper(settings, min_execution_confidence=0.0, execute_reduced_confidence=True)
    signal = make_signal()
    signal.reduced_confidence = True
    assert engine.on_signal(signal, EQUITY) is not None


def test_the_position_count_limit_holds(settings):
    engine = paper(settings, max_open_positions=2, min_execution_confidence=0.0)
    for i in range(2):
        assert engine.on_signal(make_signal(f"C{i}/USDT", id=f"s{i}"), EQUITY) is not None
    assert engine.on_signal(make_signal("C9/USDT", id="s9"), EQUITY) is None
    assert "limit is 2" in engine.decisions[-1]["reason"]


def test_the_daily_order_count_limit_holds(settings):
    engine = paper(settings, max_orders_per_day=2, max_open_positions=99,
                   min_execution_confidence=0.0)
    for i in range(2):
        engine.on_signal(make_signal(f"C{i}/USDT", id=f"s{i}"), EQUITY)
    assert engine.on_signal(make_signal("C9/USDT", id="s9"), EQUITY) is None
    assert "orders already placed today" in engine.decisions[-1]["reason"]


def test_the_daily_loss_limit_trips_and_latches(settings):
    """Recovering later in the day must not re-open the taps."""
    risk = RiskManager(replace(settings, execution_mode="paper", max_daily_loss=100.0))
    risk.record_close(-150.0)
    assert risk.state.daily_limit_tripped

    risk.record_close(+500.0)          # a winner after the limit tripped
    assert risk.state.daily_limit_tripped
    assert not risk.check(make_signal(), EQUITY)


def test_a_new_utc_day_clears_the_daily_counters(settings):
    risk = RiskManager(replace(settings, execution_mode="paper", max_daily_loss=100.0))
    risk.record_close(-150.0)
    assert risk.state.daily_limit_tripped

    tomorrow = utcnow() + timedelta(days=1)
    risk.state.roll_day(tomorrow)
    assert not risk.state.daily_limit_tripped
    assert risk.state.realized_pnl_today == 0.0


def test_the_kill_switch_survives_a_new_day(settings):
    risk = RiskManager(replace(settings, execution_mode="paper"))
    risk.kill("exit failed")
    risk.state.roll_day(utcnow() + timedelta(days=2))
    assert risk.state.killed
    assert not risk.check(make_signal(), EQUITY)


def test_the_kill_switch_reason_is_reported(settings):
    risk = RiskManager(replace(settings, execution_mode="paper"))
    risk.kill("a position may still be open")
    assert "may still be open" in risk.check(make_signal(), EQUITY).reason


# ---- sizing ----------------------------------------------------------------


def test_size_comes_from_the_distance_to_the_stop(settings):
    """Risk a fixed fraction of equity: a wider stop buys less."""
    risk = RiskManager(replace(settings, execution_mode="paper", risk_per_trade_pct=1.0,
                               max_order_notional=1e9, min_execution_confidence=0.0))
    tight = make_signal(entry=100.0)                  # stop 5 away
    tight.levels = replace(tight.levels, stop=98.0)   # now 2 away
    wide = make_signal(entry=100.0)                   # stop 5 away

    assert risk.check(tight, EQUITY).notional > risk.check(wide, EQUITY).notional


def test_the_per_order_notional_is_a_hard_cap(settings):
    risk = RiskManager(replace(settings, execution_mode="paper", risk_per_trade_pct=5.0,
                               max_order_notional=250.0, min_execution_confidence=0.0))
    assert risk.check(make_signal(), 1_000_000.0).notional == pytest.approx(250.0)


def test_zero_equity_produces_no_order(settings):
    risk = RiskManager(replace(settings, execution_mode="paper", min_execution_confidence=0.0))
    decision = risk.check(make_signal(), 0.0)
    assert not decision
    assert "size is zero" in decision.reason


def test_an_absurd_risk_fraction_is_rejected_at_startup(settings):
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        replace(settings, risk_per_trade_pct=50.0).validate()


# ---- the paper broker ------------------------------------------------------


def test_a_paper_order_records_quantity_and_notional(settings):
    engine = paper(settings, min_execution_confidence=0.0, max_order_notional=500.0,
                   risk_per_trade_pct=5.0)
    position = engine.on_signal(make_signal(entry=100.0), EQUITY)
    assert position.paper
    assert position.notional == pytest.approx(500.0)
    assert position.quantity == pytest.approx(500.0 / position.entry_price)


def test_a_paper_position_prices_its_pnl_in_the_right_direction(settings):
    engine = paper(settings, min_execution_confidence=0.0)
    long_position = engine.on_signal(make_signal("A/USDT", id="a", entry=100.0), EQUITY)
    short_position = engine.on_signal(
        make_signal("B/USDT", Direction.SHORT, id="b", entry=100.0), EQUITY)

    assert long_position.pnl(110.0) > 0
    assert short_position.pnl(110.0) < 0


def test_closing_a_paper_position_records_the_result(settings):
    engine = paper(settings, min_execution_confidence=0.0)
    signal = make_signal(entry=100.0)
    engine.on_signal(signal, EQUITY)

    update = TrackerUpdate(signal, "stop", 95.0, "stop 95 hit")
    closed = engine.on_resolution(update)
    assert not closed.is_open
    assert closed.realized_pnl < 0
    assert engine.risk.state.open_positions == 0


def test_target_one_does_not_close_the_position(settings):
    """T1 is a milestone; the signal is still running."""
    engine = paper(settings, min_execution_confidence=0.0)
    signal = make_signal()
    engine.on_signal(signal, EQUITY)

    assert engine.on_resolution(TrackerUpdate(signal, "target1", 107.5, "t1")) is None
    assert len(engine.open_positions()) == 1


def test_a_resolution_for_an_untraded_signal_is_a_no_op(settings):
    engine = paper(settings)
    assert engine.on_resolution(TrackerUpdate(make_signal(), "stop", 95.0, "x")) is None


def test_unrealized_pnl_sums_open_positions(settings):
    engine = paper(settings, min_execution_confidence=0.0, max_open_positions=5)
    engine.on_signal(make_signal("A/USDT", id="a", entry=100.0), EQUITY)
    engine.on_signal(make_signal("B/USDT", id="b", entry=100.0), EQUITY)
    assert engine.unrealized({"A/USDT": 110.0, "B/USDT": 110.0}) > 0


# ---- the live broker -------------------------------------------------------


class FakeVenue:
    def __init__(self, raise_on_create=False, raise_on_close=False):
        self.orders = []
        self.raise_on_create = raise_on_create
        self.raise_on_close = raise_on_close

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        if self.raise_on_create or (self.raise_on_close and type == "market"):
            raise RuntimeError("venue rejected the order")
        self.orders.append({"symbol": symbol, "type": type, "side": side,
                            "amount": amount, "price": price, "params": params or {}})
        return {"id": f"order-{len(self.orders)}", "filled": amount}

    def cancel_order(self, order_id, symbol):
        self.orders.append({"cancel": order_id})


def live_settings(settings):
    return replace(settings, execution_mode="live", live_confirm=LIVE_CONFIRMATION_PHRASE,
                   exchange_api_key="k", exchange_api_secret="s",
                   min_execution_confidence=0.0)


def test_a_live_entry_is_a_post_only_limit_order(settings):
    """A market order on a thin book is how a scalp becomes a donation."""
    venue = FakeVenue()
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(venue, configured), RiskManager(configured))
    engine.on_signal(make_signal(entry=100.0), EQUITY)

    order = venue.orders[0]
    assert order["type"] == "limit"
    assert order["params"].get("postOnly") is True


def test_a_long_bids_the_low_edge_of_the_entry_zone(settings):
    venue = FakeVenue()
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(venue, configured), RiskManager(configured))
    signal = make_signal(entry=100.0)
    engine.on_signal(signal, EQUITY)
    assert venue.orders[0]["price"] == pytest.approx(signal.levels.entry_low)


def test_a_short_offers_the_high_edge(settings):
    venue = FakeVenue()
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(venue, configured), RiskManager(configured))
    signal = make_signal("B/USDT", Direction.SHORT, entry=100.0)
    engine.on_signal(signal, EQUITY)
    assert venue.orders[0]["side"] == "sell"
    assert venue.orders[0]["price"] == pytest.approx(signal.levels.entry_high)


def test_a_rejected_live_order_opens_no_position(settings):
    venue = FakeVenue(raise_on_create=True)
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(venue, configured), RiskManager(configured))
    assert engine.on_signal(make_signal(), EQUITY) is None
    assert engine.risk.state.open_positions == 0


def test_a_failed_live_exit_engages_the_kill_switch(settings):
    """A position that will not close is the one case worth shouting about."""
    venue = FakeVenue(raise_on_close=True)
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(venue, configured), RiskManager(configured))
    signal = make_signal()
    engine.on_signal(signal, EQUITY)

    with pytest.raises(RuntimeError):
        engine.on_resolution(TrackerUpdate(signal, "stop", 95.0, "stop hit"))
    assert engine.risk.state.killed
    assert "may still be open" in engine.risk.state.kill_reason


def test_a_live_exit_is_a_market_order(settings):
    """Getting out is worth the spread; getting in is not."""
    venue = FakeVenue()
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(venue, configured), RiskManager(configured))
    signal = make_signal()
    engine.on_signal(signal, EQUITY)
    engine.on_resolution(TrackerUpdate(signal, "stop", 95.0, "stop hit"))

    assert venue.orders[-1]["type"] == "market"


# ---- reporting -------------------------------------------------------------


def test_every_decision_is_recorded_allowed_or_not(settings):
    engine = paper(settings, min_execution_confidence=99.0)
    engine.on_signal(make_signal(), EQUITY)
    decision = engine.decisions[-1]
    assert decision["allowed"] is False
    assert decision["symbol"] == "BTC/USDT"
    assert decision["reason"]


def test_the_snapshot_publishes_the_limits(settings):
    snapshot = paper(settings).snapshot()
    assert snapshot["broker"] == "paper"
    assert snapshot["risk"]["mode"] == "paper"
    for limit in ("max_order_notional", "max_open_positions", "max_daily_loss"):
        assert limit in snapshot["risk"]["limits"]


def test_the_snapshot_never_leaks_the_api_secret(settings):
    configured = live_settings(settings)
    engine = ExecutionEngine(configured, LiveBroker(FakeVenue(), configured),
                             RiskManager(configured))
    assert "s" not in str(engine.snapshot().get("api_secret", ""))
    assert "exchange_api_secret" not in str(engine.snapshot())


def test_risk_state_starts_clean():
    state = RiskState()
    assert not state.killed
    assert state.open_positions == 0
    assert state.realized_pnl_today == 0.0
