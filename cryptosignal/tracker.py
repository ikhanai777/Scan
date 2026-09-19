"""Grade open signals against live price.

Every call gets closed exactly one of three ways -- stop, target, or the
holding window expiring -- and every close writes a realised R. That is the
whole basis for the published hit rate, so the accounting here is deliberately
pessimistic:

* The stop is checked before the target. With only a last price per cycle we
  cannot know which came first inside the bar, and assuming the good one is
  how a backtest flatters itself.
* An expiry is marked to market at the last seen price, not to the best price
  the signal ever saw. Peak R is recorded separately, as information, never as
  a result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .models import Direction, Milestone, Signal, SignalStatus, utcnow
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackerUpdate:
    signal: Signal
    kind: str                # target1 | target | stop | expired
    price: float
    detail: str


def update_open_signals(store: Store, prices: dict[str, float],
                        now: datetime | None = None) -> list[TrackerUpdate]:
    """Advance every open signal one step. Returns the events worth alerting on."""
    now = now or utcnow()
    updates: list[TrackerUpdate] = []

    for signal in store.open_signals():
        price = prices.get(signal.symbol)
        if price is None or price <= 0:
            # No fresh price this cycle. The signal stays open; an expiry that
            # falls in a data gap is handled on the next cycle that has a price.
            continue

        update = _advance(signal, price, now)
        store.update_outcome(signal)
        if update is not None:
            store.add_event(signal.id, update.kind, price, update.detail)
            updates.append(update)

    return updates


def _advance(signal: Signal, price: float, now: datetime) -> TrackerUpdate | None:
    levels = signal.levels
    r_now = signal.unrealized_r(price)
    signal.peak_r = max(signal.peak_r, r_now)
    signal.trough_r = min(signal.trough_r, r_now)

    if _crossed(signal.direction, price, levels.stop, adverse=True):
        return _close(signal, price, now, SignalStatus.CLOSED_STOP, "stop",
                      f"stop {levels.stop:.6g} hit")

    if _crossed(signal.direction, price, levels.target2, adverse=False):
        return _close(signal, price, now, SignalStatus.CLOSED_TARGET, "target",
                      f"target 2 {levels.target2:.6g} hit")

    if signal.status is SignalStatus.OPEN and _crossed(signal.direction, price, levels.target1, adverse=False):
        signal.status = SignalStatus.TARGET1
        signal.target1_hit_at = now
        detail = f"target 1 {levels.target1:.6g} hit, running to target 2"
        signal.milestones = (*signal.milestones, Milestone("target1", now, price, detail))
        return TrackerUpdate(signal, "target1", price, detail)

    if now >= signal.expires_at:
        # The realised R is a field of its own; repeating it in the reason
        # makes every card and alert print the same number twice.
        return _close(signal, price, now, SignalStatus.CLOSED_EXPIRED, "expired",
                      "holding window expired")

    return None


def _crossed(direction: Direction, price: float, level: float, *, adverse: bool) -> bool:
    """Has price reached `level` on the side that matters for this direction?"""
    if direction is Direction.LONG:
        return price <= level if adverse else price >= level
    return price >= level if adverse else price <= level


def _close(signal: Signal, price: float, now: datetime, status: SignalStatus,
           kind: str, detail: str) -> TrackerUpdate:
    signal.status = status
    signal.closed_at = now
    signal.close_price = price
    signal.close_reason = detail
    signal.milestones = (*signal.milestones, Milestone(kind, now, price, detail))
    # Graded at the level for a stop or target -- that is where the exit sits --
    # and marked to market for an expiry.
    if status is SignalStatus.CLOSED_STOP:
        signal.realized_r = signal.unrealized_r(signal.levels.stop)
    elif status is SignalStatus.CLOSED_TARGET:
        signal.realized_r = signal.unrealized_r(signal.levels.target2)
    else:
        signal.realized_r = signal.unrealized_r(price)
    log.info("signal %s %s: %s (%.2fR)", signal.id, kind, detail, signal.realized_r)
    return TrackerUpdate(signal, kind, price, detail)
