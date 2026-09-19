"""The shapes that move between layers.

Ingestion produces `Candles`, screening produces `Candidate`, each analysis leg
produces a `LegScore`, and fusion turns those into a `Signal`. Nothing here
reaches out to a network or a database; that keeps the scoring path testable
against synthetic series.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

_TIMEFRAME_RE = re.compile(r"^(\d+)([mhdw])$")
_TIMEFRAME_UNIT_MINUTES = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}


def timeframe_minutes(timeframe: str) -> int:
    """'15m' -> 15. Raises on anything ccxt would also reject."""
    match = _TIMEFRAME_RE.match(timeframe.strip().lower())
    if not match:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    return amount * _TIMEFRAME_UNIT_MINUTES[unit]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class SignalStatus(StrEnum):
    OPEN = "open"
    TARGET1 = "target1"          # first target hit, still running to target 2
    CLOSED_TARGET = "closed_target"
    CLOSED_STOP = "closed_stop"
    CLOSED_EXPIRED = "closed_expired"

    @property
    def is_open(self) -> bool:
        return self in (SignalStatus.OPEN, SignalStatus.TARGET1)


class LegName(StrEnum):
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    SENTIMENT = "sentiment"


@dataclass(frozen=True)
class Candles:
    """OHLCV for one symbol, oldest first, as parallel sequences.

    ccxt hands back a list of rows; the indicator code wants columns. Doing the
    transpose once, here, keeps every indicator free of row indexing.
    """

    symbol: str
    timeframe: str
    timestamps: tuple[int, ...]      # milliseconds, UTC
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    volume: tuple[float, ...]

    def __post_init__(self) -> None:
        lengths = {
            len(self.timestamps), len(self.open), len(self.high),
            len(self.low), len(self.close), len(self.volume),
        }
        if len(lengths) != 1:
            raise ValueError(f"{self.symbol}: OHLCV columns have mismatched lengths {sorted(lengths)}")

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_rows(cls, symbol: str, timeframe: str, rows: Sequence[Sequence[float]]) -> Candles:
        """Build from ccxt's [[ts, o, h, l, c, v], ...], dropping malformed rows.

        An exchange occasionally returns a row with a null volume or a gap.
        Dropping it beats propagating a NaN through every downstream indicator.
        """
        clean: list[Sequence[float]] = []
        for row in rows:
            if len(row) < 6:
                continue
            if any(v is None for v in row[:6]):
                continue
            if any(isinstance(v, float) and not math.isfinite(v) for v in row[:6]):
                continue
            if row[2] < row[3]:          # high below low: corrupt
                continue
            clean.append(row)
        clean.sort(key=lambda r: r[0])
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            timestamps=tuple(int(r[0]) for r in clean),
            open=tuple(float(r[1]) for r in clean),
            high=tuple(float(r[2]) for r in clean),
            low=tuple(float(r[3]) for r in clean),
            close=tuple(float(r[4]) for r in clean),
            volume=tuple(float(r[5]) for r in clean),
        )

    @property
    def last_close(self) -> float:
        return self.close[-1]

    @property
    def last_timestamp(self) -> int:
        return self.timestamps[-1]

    def age_seconds(self, now: datetime | None = None) -> float:
        """How long since the most recent candle opened."""
        now = now or utcnow()
        return now.timestamp() - self.last_timestamp / 1000.0


@dataclass(frozen=True)
class MarketSnapshot:
    """The stage 1 view of a market: liquidity and cost of entry."""

    symbol: str
    base: str
    quote: str
    last: float
    quote_volume_24h: float
    spread_bps: float
    change_24h_pct: float


@dataclass(frozen=True)
class Candidate:
    """A market that survived stage 1 and earned a stage 2 setup score."""

    market: MarketSnapshot
    setup_score: float                       # 0..100, direction-agnostic
    components: dict[str, float]             # the parts that built it
    reasons: tuple[str, ...] = ()

    @property
    def symbol(self) -> str:
        return self.market.symbol


@dataclass(frozen=True)
class Driver:
    """One reason the score is what it is. The card shows the top three."""

    label: str
    score: float                 # -100..+100, this driver's own read
    weight: float                # its share of the leg
    detail: str = ""

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass(frozen=True)
class LegScore:
    """One analysis leg's verdict: -100 strong short .. +100 strong long."""

    leg: LegName
    score: float
    drivers: tuple[Driver, ...] = ()
    # False when the leg had no usable data. Fusion renormalises over the legs
    # that did report, which is how phase 1 runs technical-only without any
    # weight surgery.
    available: bool = True
    note: str = ""

    def top_drivers(self, count: int = 3) -> tuple[Driver, ...]:
        """Drivers ranked by how much they actually moved the leg score."""
        ranked = sorted(self.drivers, key=lambda d: abs(d.contribution), reverse=True)
        return tuple(ranked[:count])


@dataclass(frozen=True)
class Levels:
    entry_low: float
    entry_high: float
    stop: float
    target1: float
    target2: float
    hold_minutes: int

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_mid - self.stop)

    def reward_risk(self, target: float) -> float:
        risk = self.risk_per_unit
        return abs(target - self.entry_mid) / risk if risk > 0 else 0.0


@dataclass(frozen=True)
class Milestone:
    """One timestamped price event in a signal's life."""

    kind: str                 # fired | target1 | target | stop | expired
    at: datetime
    price: float
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "price": self.price,
            "detail": self.detail,
        }


@dataclass
class Signal:
    """A fired call, and the record we later grade it against."""

    id: str
    symbol: str
    base: str
    direction: Direction
    composite_score: float
    confidence: float
    levels: Levels
    drivers: tuple[Driver, ...]
    leg_scores: dict[str, float]
    reduced_confidence: bool
    reduced_confidence_reason: str
    reference_price: float
    setup_score: float
    created_at: datetime
    expires_at: datetime
    status: SignalStatus = SignalStatus.OPEN
    # Filled in by the tracker as the signal plays out.
    closed_at: datetime | None = None
    close_price: float | None = None
    close_reason: str = ""
    realized_r: float | None = None
    peak_r: float = 0.0
    trough_r: float = 0.0
    target1_hit_at: datetime | None = None
    # Every price event, in order: what happened, when, and at what price.
    # `created_at` alone cannot answer "when did it hit the stop, and where was
    # price then" -- and that is the first question anyone asks of a closed
    # signal. Mirrors the backtest's fill timeline, so a live trade and a
    # replayed one read the same way.
    milestones: tuple[Milestone, ...] = ()
    #: What the market looked like and what the timeframe above was doing.
    #: "fired in a chop" is something a trader wants to see before sizing.
    regime: str = ""
    htf_note: str = ""
    notes: str = ""

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    def minutes_remaining(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return (self.expires_at - now).total_seconds() / 60.0

    def unrealized_r(self, price: float) -> float:
        """Where price sits, measured in units of the signal's own risk."""
        risk = self.levels.risk_per_unit
        if risk <= 0:
            return 0.0
        return self.direction.sign * (price - self.levels.entry_mid) / risk

    def to_public_dict(self) -> dict[str, object]:
        """The signal card, as the dashboard and the alert channels see it."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "base": self.base,
            "direction": self.direction.value,
            "confidence": round(self.confidence, 1),
            "composite_score": round(self.composite_score, 1),
            "setup_score": round(self.setup_score, 1),
            "reduced_confidence": self.reduced_confidence,
            "reduced_confidence_reason": self.reduced_confidence_reason,
            "reference_price": self.reference_price,
            "entry_low": self.levels.entry_low,
            "entry_high": self.levels.entry_high,
            "stop": self.levels.stop,
            "target1": self.levels.target1,
            "target2": self.levels.target2,
            "rr_target1": round(self.levels.reward_risk(self.levels.target1), 2),
            "rr_target2": round(self.levels.reward_risk(self.levels.target2), 2),
            "hold_minutes": self.levels.hold_minutes,
            "drivers": [
                {"label": d.label, "score": round(d.score, 1), "detail": d.detail}
                for d in self.drivers
            ],
            "leg_scores": {k: round(v, 1) for k, v in self.leg_scores.items()},
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status.value,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "close_price": self.close_price,
            "close_reason": self.close_reason,
            "realized_r": round(self.realized_r, 2) if self.realized_r is not None else None,
            "peak_r": round(self.peak_r, 2),
            "trough_r": round(self.trough_r, 2),
            "target1_hit_at": self.target1_hit_at.isoformat() if self.target1_hit_at else None,
            "minutes_held": round(self.minutes_held, 1) if self.minutes_held is not None else None,
            "milestones": [m.to_dict() for m in self.milestones],
            "regime": self.regime,
            "htf_note": self.htf_note,
        }

    @property
    def minutes_held(self) -> float | None:
        """Wall-clock life of the signal, once it has closed."""
        if self.closed_at is None:
            return None
        return (self.closed_at - self.created_at).total_seconds() / 60.0


@dataclass(frozen=True)
class ScanReport:
    """What one cycle did, for the dashboard's status bar and the log."""

    started_at: datetime
    finished_at: datetime
    markets_seen: int
    passed_stage1: int
    shortlisted: int
    analysed: int
    signals_fired: int
    fetch_failures: int
    fetch_attempts: int
    halted: bool = False
    halt_reason: str = ""
    candidates: tuple[Candidate, ...] = field(default=(), repr=False)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def failure_ratio(self) -> float:
        return self.fetch_failures / self.fetch_attempts if self.fetch_attempts else 0.0

    def to_dict(self) -> dict[str, object]:
        data = {k: v for k, v in asdict(self).items() if k != "candidates"}
        data["started_at"] = self.started_at.isoformat()
        data["finished_at"] = self.finished_at.isoformat()
        data["duration_seconds"] = round(self.duration_seconds, 2)
        data["failure_ratio"] = round(self.failure_ratio, 3)
        # The shortlist is what the dashboard shows as "watching now", so it
        # travels with the report rather than living only in the scanner's
        # memory -- the API may well be a different process.
        data["shortlist"] = [
            {
                "symbol": c.symbol,
                "setup_score": c.setup_score,
                "components": c.components,
                "reasons": list(c.reasons),
                "last": c.market.last,
                "change_24h_pct": c.market.change_24h_pct,
                "quote_volume_24h": c.market.quote_volume_24h,
            }
            for c in self.candidates
        ]
        return data


def expiry_for(created_at: datetime, hold_minutes: int) -> datetime:
    return created_at + timedelta(minutes=hold_minutes)
