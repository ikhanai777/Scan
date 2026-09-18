"""Phase 5: turning a signal into an order, under strict opt-in controls.

The spec marks this phase optional and gates it on "strict, opt-in risk
controls". Both words are load-bearing here:

* **Opt-in.** The default is `disabled`. Nothing in this package runs unless
  `CS_EXECUTION_MODE` says so, and `live` additionally requires a confirmation
  phrase that nobody sets by accident.
* **Strict.** Every order passes `RiskManager.check`, which denies by default.

The engine below is the only thing that connects the scanner to a broker, and
it is deliberately thin: decide, size, place, record. All judgement lives in
`risk.py`, all venue contact in `broker.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..models import Signal, utcnow
from ..tracker import TrackerUpdate
from .broker import Broker, LiveBroker, PaperBroker, Position
from .risk import LIVE_CONFIRMATION_PHRASE, ExecutionMode, RiskManager

log = logging.getLogger(__name__)

__all__ = [
    "ExecutionEngine", "RiskManager", "ExecutionMode", "PaperBroker", "LiveBroker",
    "Position", "LIVE_CONFIRMATION_PHRASE", "build_engine",
]


class ExecutionEngine:
    """Signals in, orders out -- when, and only when, risk allows."""

    def __init__(self, settings: Settings, broker: Broker, risk: RiskManager) -> None:
        self.settings = settings
        self.broker = broker
        self.risk = risk
        self.positions: dict[str, Position] = {}
        #: Every decision, allowed or refused, for the dashboard and the log.
        self.decisions: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self.risk.mode != ExecutionMode.DISABLED

    def on_signal(self, signal: Signal, equity: float) -> Position | None:
        """Called when a signal fires. Returns the position, or None if refused."""
        decision = self.risk.check(signal, equity)
        self._record(signal, decision.allowed, decision.reason, decision.notional)
        if not decision:
            log.info("execution refused %s: %s", signal.symbol, decision.reason)
            return None

        position = self.broker.open(signal, decision.notional)
        if position is None:
            self._record(signal, False, "broker did not fill the order", decision.notional)
            return None

        self.positions[signal.id] = position
        self.risk.record_fill(decision.notional)
        return position

    def on_resolution(self, update: TrackerUpdate) -> Position | None:
        """Called when the tracker closes a signal. Exits the matching position."""
        position = self.positions.get(update.signal.id)
        if position is None or not position.is_open:
            return None
        if update.kind == "target1":
            # Target 1 is a milestone, not an exit -- the signal runs on.
            return None

        try:
            closed = self.broker.close(position, update.price, update.detail)
        except Exception:
            self.risk.kill(f"exit failed for {position.symbol}; a position may still be open")
            raise

        self.risk.record_close(closed.realized_pnl or 0.0)
        return closed

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for position in self.open_positions():
            price = prices.get(position.symbol)
            if price:
                total += position.pnl(price)
        return total

    def snapshot(self) -> dict[str, Any]:
        return {
            "broker": self.broker.name,
            "enabled": self.enabled,
            "risk": self.risk.snapshot(),
            "open": [p.to_dict() for p in self.open_positions()],
            "recent_decisions": self.decisions[-20:],
        }

    def _record(self, signal: Signal, allowed: bool, reason: str, notional: float) -> None:
        self.decisions.append({
            "at": utcnow().isoformat(),
            "signal_id": signal.id,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "allowed": allowed,
            "reason": reason,
            "notional": round(notional, 2),
        })


def build_engine(settings: Settings, exchange_client: Any | None = None) -> ExecutionEngine | None:
    """Construct the engine for the configured mode, or None when disabled.

    Raises on a live configuration that is incomplete. That is deliberate: a
    process told to trade real money must not start in a half-configured state
    and quietly do something else.
    """
    risk = RiskManager(settings)
    problem = risk.startup_error()
    if problem:
        raise RuntimeError(problem)

    mode = risk.mode
    if mode == ExecutionMode.DISABLED:
        return None
    if mode == ExecutionMode.PAPER:
        log.info("execution: PAPER mode -- orders are recorded, nothing is sent to a venue")
        return ExecutionEngine(settings, PaperBroker(), risk)

    if exchange_client is None:
        raise RuntimeError("live execution needs an authenticated exchange client")
    log.warning("execution: LIVE mode -- this process will place real orders with real money")
    return ExecutionEngine(settings, LiveBroker(exchange_client, settings), risk)
