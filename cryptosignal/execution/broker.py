"""Placing orders: paper against real prices, or live through ccxt.

Both brokers implement the same interface, so the scanner cannot tell them
apart and there is no branch anywhere else in the codebase that asks "are we
live". The difference lives here and only here.

**PaperBroker** is not a simulation of a market. It records the order against
the real price the signal fired at, marks it to the venue's real prices as they
arrive, and closes it on the same stop/target/expiry logic the tracker uses for
signals. It does not model slippage, queue position or partial fills, and it
says so rather than implying a fidelity it does not have -- a paper fill is the
*best case*, and the gap between it and a live fill is exactly the cost of
finding out for real.

**LiveBroker** places limit orders inside the signal's entry zone, never market
orders. A market order on a thin book is how a scalp becomes a donation. An
order that does not fill inside the entry window is cancelled rather than
chased.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..models import Direction, Signal, utcnow

log = logging.getLogger(__name__)


@dataclass
class Position:
    """An order that filled, and what it is worth now."""

    signal_id: str
    symbol: str
    direction: Direction
    quantity: float
    entry_price: float
    notional: float
    opened_at: datetime
    order_id: str = ""
    paper: bool = True
    closed_at: datetime | None = None
    exit_price: float | None = None
    close_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def pnl(self, price: float) -> float:
        return self.direction.sign * (price - self.entry_price) * self.quantity

    @property
    def realized_pnl(self) -> float | None:
        return None if self.exit_price is None else self.pnl(self.exit_price)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "notional": round(self.notional, 2),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "exit_price": self.exit_price,
            "close_reason": self.close_reason,
            "realized_pnl": round(self.realized_pnl, 2) if self.realized_pnl is not None else None,
            "paper": self.paper,
            "order_id": self.order_id,
        }


class Broker(Protocol):
    name: str

    def open(self, signal: Signal, notional: float) -> Position | None: ...
    def close(self, position: Position, price: float, reason: str) -> Position: ...


@dataclass
class PaperBroker:
    """Records orders against real prices without sending anything anywhere."""

    name: str = "paper"
    positions: dict[str, Position] = field(default_factory=dict)

    def open(self, signal: Signal, notional: float) -> Position | None:
        entry = signal.levels.entry_mid
        if entry <= 0 or notional <= 0:
            return None
        position = Position(
            signal_id=signal.id, symbol=signal.symbol, direction=signal.direction,
            quantity=notional / entry, entry_price=entry, notional=notional,
            opened_at=utcnow(), order_id=f"paper-{signal.id}", paper=True,
        )
        self.positions[signal.id] = position
        log.info("PAPER OPEN %s %s %.6g @ %.6g (%.2f notional)",
                 signal.direction.value.upper(), signal.symbol,
                 position.quantity, entry, notional)
        return position

    def close(self, position: Position, price: float, reason: str) -> Position:
        position.closed_at = utcnow()
        position.exit_price = price
        position.close_reason = reason
        log.info("PAPER CLOSE %s @ %.6g -- %s (%.2f pnl)",
                 position.symbol, price, reason, position.realized_pnl or 0.0)
        return position


class LiveBroker:
    """Real orders, through ccxt, with a key that must carry trade permission.

    Nothing in this class widens what `RiskManager` allowed: it receives an
    already-approved notional and places exactly that.
    """

    name = "live"

    def __init__(self, client: Any, settings) -> None:
        self._client = client
        self.settings = settings
        self.positions: dict[str, Position] = {}

    def open(self, signal: Signal, notional: float) -> Position | None:
        entry = self._limit_price(signal)
        if entry <= 0 or notional <= 0:
            return None
        quantity = notional / entry
        side = "buy" if signal.direction is Direction.LONG else "sell"

        try:
            order = self._client.create_order(
                symbol=signal.symbol, type="limit", side=side,
                amount=quantity, price=entry,
                # Post-only keeps this a maker order: it is cancelled rather
                # than crossing the spread, which is the whole point of not
                # using a market order.
                params={"postOnly": True},
            )
        except Exception as exc:
            log.error("LIVE order REJECTED for %s: %s", signal.symbol, exc)
            return None

        order_id = str(order.get("id", "")) if isinstance(order, dict) else ""
        filled = _number(order.get("filled") if isinstance(order, dict) else None)
        if not filled:
            log.warning("LIVE order %s placed but unfilled; it will be cancelled if it stays so",
                        order_id or "?")

        position = Position(
            signal_id=signal.id, symbol=signal.symbol, direction=signal.direction,
            quantity=filled or quantity, entry_price=entry, notional=notional,
            opened_at=utcnow(), order_id=order_id, paper=False,
        )
        self.positions[signal.id] = position
        log.warning("LIVE OPEN %s %s %.6g @ %.6g (order %s)",
                    side.upper(), signal.symbol, position.quantity, entry, order_id)
        return position

    def close(self, position: Position, price: float, reason: str) -> Position:
        side = "sell" if position.direction is Direction.LONG else "buy"
        try:
            self._client.create_order(
                symbol=position.symbol, type="market", side=side,
                amount=position.quantity,
            )
        except Exception as exc:
            # An exit that will not go through is the one case worth shouting
            # about: the position is still on and nobody is watching it.
            log.error("LIVE EXIT FAILED for %s (%s) -- POSITION MAY STILL BE OPEN: %s",
                      position.symbol, reason, exc)
            raise

        position.closed_at = utcnow()
        position.exit_price = price
        position.close_reason = reason
        log.warning("LIVE CLOSE %s @ %.6g -- %s", position.symbol, price, reason)
        return position

    def cancel_unfilled(self, position: Position) -> None:
        if not position.order_id:
            return
        try:
            self._client.cancel_order(position.order_id, position.symbol)
            log.info("cancelled unfilled order %s", position.order_id)
        except Exception as exc:
            log.warning("could not cancel order %s: %s", position.order_id, exc)

    def _limit_price(self, signal: Signal) -> float:
        """The patient edge of the entry zone: buy the low, sell the high."""
        if signal.direction is Direction.LONG:
            return signal.levels.entry_low
        return signal.levels.entry_high


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None
