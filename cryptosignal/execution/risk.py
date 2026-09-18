"""The limits that stand between a signal and an order.

Phase 5 of the spec is "auto-execution with strict, opt-in risk controls". This
module is the strict part, and it is written to be boring and total: every
order passes through `RiskManager.check`, and anything it does not explicitly
allow is refused.

Four properties matter more than the individual numbers:

* **Default deny.** Live trading requires `CS_EXECUTION_MODE=live` *and*
  `CS_LIVE_CONFIRM` set to the exact phrase. Two independent settings, because
  one environment variable is too easy to set by accident in a deploy config.
* **Every limit is absolute, not advisory.** A refusal is a refusal; there is
  no "warn and proceed" path anywhere in this file.
* **The daily loss limit latches.** Once tripped it stays tripped for the rest
  of the UTC day even if later trades recover, because the point is to stop a
  bad day from becoming a worse one, not to track a running total.
* **The kill switch is one-way** within a process. Nothing re-arms it
  automatically; a human restarts the process.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import Settings
from ..models import Signal, utcnow

log = logging.getLogger(__name__)

#: The exact phrase CS_LIVE_CONFIRM must carry for live orders to be allowed.
LIVE_CONFIRMATION_PHRASE = "I understand this places real orders"


class ExecutionMode:
    DISABLED = "disabled"
    PAPER = "paper"
    LIVE = "live"

    ALL = (DISABLED, PAPER, LIVE)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    notional: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class RiskState:
    """Mutable running state: the day's damage, and the kill switch."""

    day: date = field(default_factory=lambda: utcnow().date())
    realized_pnl_today: float = 0.0
    orders_today: int = 0
    open_positions: int = 0
    killed: bool = False
    kill_reason: str = ""
    daily_limit_tripped: bool = False

    def roll_day(self, now: datetime | None = None) -> None:
        """A new UTC day resets the day's counters -- but never the kill switch."""
        today = (now or utcnow()).date()
        if today != self.day:
            self.day = today
            self.realized_pnl_today = 0.0
            self.orders_today = 0
            self.daily_limit_tripped = False


class RiskManager:
    """Every order goes through `check`. Nothing bypasses it."""

    def __init__(self, settings: Settings, state: RiskState | None = None) -> None:
        self.settings = settings
        self.state = state or RiskState()

    # -- mode --------------------------------------------------------------

    @property
    def mode(self) -> str:
        """The effective mode, after the live-confirmation gate.

        A configuration that asks for live without the confirmation phrase does
        not fall back to paper silently -- it is refused outright, because
        silently paper-trading someone who believes they are live is its own
        kind of failure.
        """
        configured = (self.settings.execution_mode or ExecutionMode.DISABLED).strip().lower()
        if configured not in ExecutionMode.ALL:
            return ExecutionMode.DISABLED
        return configured

    @property
    def live_confirmed(self) -> bool:
        return self.settings.live_confirm.strip() == LIVE_CONFIRMATION_PHRASE

    def startup_error(self) -> str | None:
        """A configuration that must stop the process, rather than run wrong."""
        if self.mode != ExecutionMode.LIVE:
            return None
        if not self.live_confirmed:
            return (
                "CS_EXECUTION_MODE=live requires CS_LIVE_CONFIRM to be set to exactly:\n"
                f'    "{LIVE_CONFIRMATION_PHRASE}"\n'
                "Refusing to start. This is deliberate: live mode places real orders "
                "with real money, and one environment variable is too easy to set by accident."
            )
        if not (self.settings.exchange_api_key and self.settings.exchange_api_secret):
            return ("live mode needs CS_EXCHANGE_API_KEY and CS_EXCHANGE_API_SECRET. "
                    "Refusing to start.")
        if self.settings.max_order_notional <= 0:
            return "live mode needs a positive CS_MAX_ORDER_NOTIONAL. Refusing to start."
        return None

    # -- the gate ----------------------------------------------------------

    def check(self, signal: Signal, equity: float, now: datetime | None = None) -> RiskDecision:
        """May this signal become an order, and for how much?"""
        now = now or utcnow()
        self.state.roll_day(now)

        if self.state.killed:
            return RiskDecision(False, f"kill switch engaged: {self.state.kill_reason}")

        mode = self.mode
        if mode == ExecutionMode.DISABLED:
            return RiskDecision(False, "execution is disabled (CS_EXECUTION_MODE=disabled)")
        if mode == ExecutionMode.LIVE and not self.live_confirmed:
            return RiskDecision(False, "live mode is not confirmed; refusing to place an order")

        if self.state.daily_limit_tripped:
            return RiskDecision(False, "daily loss limit already tripped today")

        if self.settings.max_daily_loss > 0 and self.state.realized_pnl_today <= -abs(self.settings.max_daily_loss):
            self.state.daily_limit_tripped = True
            return RiskDecision(False, (
                f"daily loss limit hit: {self.state.realized_pnl_today:,.2f} "
                f"against a {self.settings.max_daily_loss:,.2f} limit"
            ))

        if self.state.open_positions >= self.settings.max_open_positions:
            return RiskDecision(False, (
                f"already holding {self.state.open_positions} position(s), "
                f"limit is {self.settings.max_open_positions}"
            ))

        if self.settings.max_orders_per_day > 0 and self.state.orders_today >= self.settings.max_orders_per_day:
            return RiskDecision(False, (
                f"{self.state.orders_today} orders already placed today, "
                f"limit is {self.settings.max_orders_per_day}"
            ))

        if signal.confidence < self.settings.min_execution_confidence:
            return RiskDecision(False, (
                f"confidence {signal.confidence:.0f}% is below the execution floor "
                f"of {self.settings.min_execution_confidence:.0f}%"
            ))

        if signal.reduced_confidence and not self.settings.execute_reduced_confidence:
            return RiskDecision(False, (
                "signal is flagged reduced confidence and "
                "CS_EXECUTE_REDUCED_CONFIDENCE is off"
            ))

        notional = self._size(signal, equity)
        if notional <= 0:
            return RiskDecision(False, "computed position size is zero")
        return RiskDecision(True, "within every limit", notional)

    def _size(self, signal: Signal, equity: float) -> float:
        """Risk a fixed fraction of equity, capped by the per-order notional.

        Size comes from the distance to the stop, so a wider stop buys less.
        This is the one place position size is decided, and it is why v1's
        signal cards deliberately carry no size at all.
        """
        risk_fraction = self.settings.risk_per_trade_pct / 100.0
        risk_budget = max(0.0, equity) * risk_fraction
        entry = signal.levels.entry_mid
        stop_distance = abs(entry - signal.levels.stop)
        if stop_distance <= 0 or entry <= 0:
            return 0.0

        units = risk_budget / stop_distance
        notional = units * entry
        return float(min(notional, self.settings.max_order_notional))

    # -- feedback ----------------------------------------------------------

    def record_fill(self, notional: float) -> None:
        self.state.orders_today += 1
        self.state.open_positions += 1
        log.info("risk: %d open position(s), %d order(s) today, %.2f notional",
                 self.state.open_positions, self.state.orders_today, notional)

    def record_close(self, realized_pnl: float, now: datetime | None = None) -> None:
        self.state.roll_day(now)
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.realized_pnl_today += realized_pnl
        if (self.settings.max_daily_loss > 0
                and self.state.realized_pnl_today <= -abs(self.settings.max_daily_loss)):
            self.state.daily_limit_tripped = True
            log.error("risk: daily loss limit tripped at %.2f -- no further orders today",
                      self.state.realized_pnl_today)

    def kill(self, reason: str) -> None:
        """One-way within this process. A human restarts to clear it."""
        self.state.killed = True
        self.state.kill_reason = reason
        log.error("risk: KILL SWITCH ENGAGED -- %s", reason)

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "live_confirmed": self.live_confirmed,
            "killed": self.state.killed,
            "kill_reason": self.state.kill_reason,
            "open_positions": self.state.open_positions,
            "orders_today": self.state.orders_today,
            "realized_pnl_today": round(self.state.realized_pnl_today, 2),
            "daily_limit_tripped": self.state.daily_limit_tripped,
            "limits": {
                "max_order_notional": self.settings.max_order_notional,
                "max_open_positions": self.settings.max_open_positions,
                "max_orders_per_day": self.settings.max_orders_per_day,
                "max_daily_loss": self.settings.max_daily_loss,
                "risk_per_trade_pct": self.settings.risk_per_trade_pct,
                "min_execution_confidence": self.settings.min_execution_confidence,
            },
        }
