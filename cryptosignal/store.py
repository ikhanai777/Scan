"""SQLite persistence for signals, their outcomes, and each scan cycle.

The spec asks for outcome tracking so the model's own win rate and drawdown can
be published rather than implied. That only works if every signal is written
down at the moment it fires, with the levels it fired at -- grading a call
against levels you edited afterwards is grading nothing. So signals are
inserted once and only their outcome columns are ever updated.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    Direction,
    Driver,
    Levels,
    ScanReport,
    Signal,
    SignalStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id                        TEXT PRIMARY KEY,
    symbol                    TEXT NOT NULL,
    base                      TEXT NOT NULL,
    direction                 TEXT NOT NULL,
    composite_score           REAL NOT NULL,
    confidence                REAL NOT NULL,
    setup_score               REAL NOT NULL,
    reference_price           REAL NOT NULL,
    entry_low                 REAL NOT NULL,
    entry_high                REAL NOT NULL,
    stop                      REAL NOT NULL,
    target1                   REAL NOT NULL,
    target2                   REAL NOT NULL,
    hold_minutes              INTEGER NOT NULL,
    reduced_confidence        INTEGER NOT NULL,
    reduced_confidence_reason TEXT NOT NULL DEFAULT '',
    drivers_json              TEXT NOT NULL DEFAULT '[]',
    leg_scores_json           TEXT NOT NULL DEFAULT '{}',
    created_at                TEXT NOT NULL,
    expires_at                TEXT NOT NULL,
    status                    TEXT NOT NULL,
    closed_at                 TEXT,
    close_price               REAL,
    close_reason              TEXT NOT NULL DEFAULT '',
    realized_r                REAL,
    peak_r                    REAL NOT NULL DEFAULT 0,
    trough_r                  REAL NOT NULL DEFAULT 0,
    target1_hit_at            TEXT,
    notes                     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol  ON signals(symbol);

CREATE TABLE IF NOT EXISTS signal_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL REFERENCES signals(id),
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    price     REAL,
    detail    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_signal ON signal_events(signal_id, at);

CREATE TABLE IF NOT EXISTS scan_cycles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    report_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cycles_started ON scan_cycles(started_at DESC);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    # Rows written before a timezone was attached would otherwise compare
    # against aware datetimes and raise.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Store:
    def __init__(self, path: str = "cryptosignal.db") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        with self._transaction() as cursor:
            cursor.executescript(SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._connection.cursor()
        try:
            yield cursor
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            cursor.close()

    def close(self) -> None:
        self._connection.close()

    # -- signals ----------------------------------------------------------

    def insert_signal(self, signal: Signal) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO signals (
                    id, symbol, base, direction, composite_score, confidence, setup_score,
                    reference_price, entry_low, entry_high, stop, target1, target2,
                    hold_minutes, reduced_confidence, reduced_confidence_reason,
                    drivers_json, leg_scores_json, created_at, expires_at, status,
                    closed_at, close_price, close_reason, realized_r, peak_r, trough_r,
                    target1_hit_at, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    signal.id, signal.symbol, signal.base, signal.direction.value,
                    signal.composite_score, signal.confidence, signal.setup_score,
                    signal.reference_price, signal.levels.entry_low, signal.levels.entry_high,
                    signal.levels.stop, signal.levels.target1, signal.levels.target2,
                    signal.levels.hold_minutes, int(signal.reduced_confidence),
                    signal.reduced_confidence_reason,
                    json.dumps([{"label": d.label, "score": d.score, "weight": d.weight, "detail": d.detail}
                                for d in signal.drivers]),
                    json.dumps(signal.leg_scores),
                    _iso(signal.created_at), _iso(signal.expires_at), signal.status.value,
                    _iso(signal.closed_at), signal.close_price, signal.close_reason,
                    signal.realized_r, signal.peak_r, signal.trough_r,
                    _iso(signal.target1_hit_at), signal.notes,
                ),
            )
        self.add_event(signal.id, "fired", signal.reference_price,
                       f"{signal.direction.value} @ {signal.confidence:.0f}% confidence")

    def update_outcome(self, signal: Signal) -> None:
        """Only the columns the tracker owns. The call itself is immutable."""
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE signals SET status=?, closed_at=?, close_price=?, close_reason=?,
                       realized_r=?, peak_r=?, trough_r=?, target1_hit_at=?, notes=?
                 WHERE id=?
                """,
                (
                    signal.status.value, _iso(signal.closed_at), signal.close_price,
                    signal.close_reason, signal.realized_r, signal.peak_r, signal.trough_r,
                    _iso(signal.target1_hit_at), signal.notes, signal.id,
                ),
            )

    def add_event(self, signal_id: str, kind: str, price: float | None, detail: str = "") -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO signal_events (signal_id, at, kind, price, detail) VALUES (?,?,?,?,?)",
                (signal_id, datetime.now(UTC).isoformat(), kind, price, detail),
            )

    def events_for(self, signal_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT at, kind, price, detail FROM signal_events WHERE signal_id=? ORDER BY at, id",
            (signal_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def open_signals(self) -> list[Signal]:
        rows = self._connection.execute(
            "SELECT * FROM signals WHERE status IN (?,?) ORDER BY created_at DESC",
            (SignalStatus.OPEN.value, SignalStatus.TARGET1.value),
        ).fetchall()
        return [_row_to_signal(row) for row in rows]

    def open_symbols(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT symbol FROM signals WHERE status IN (?,?)",
            (SignalStatus.OPEN.value, SignalStatus.TARGET1.value),
        ).fetchall()
        return {row["symbol"] for row in rows}

    def get_signal(self, signal_id: str) -> Signal | None:
        row = self._connection.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return _row_to_signal(row) if row else None

    def recent_signals(self, limit: int = 50, status: str | None = None,
                       direction: str | None = None, symbol: str | None = None,
                       min_confidence: float | None = None) -> list[Signal]:
        clauses, params = [], []
        if status == "open":
            clauses.append("status IN (?,?)")
            params.extend([SignalStatus.OPEN.value, SignalStatus.TARGET1.value])
        elif status == "closed":
            clauses.append("status NOT IN (?,?)")
            params.extend([SignalStatus.OPEN.value, SignalStatus.TARGET1.value])
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if min_confidence is not None:
            clauses.append("confidence >= ?")
            params.append(min_confidence)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM signals {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        return [_row_to_signal(row) for row in rows]

    # -- cycles -----------------------------------------------------------

    def record_cycle(self, report: ScanReport) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO scan_cycles (started_at, finished_at, report_json) VALUES (?,?,?)",
                (_iso(report.started_at), _iso(report.finished_at), json.dumps(report.to_dict())),
            )

    def last_cycle(self) -> dict | None:
        row = self._connection.execute(
            "SELECT report_json FROM scan_cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["report_json"]) if row else None

    # -- performance ------------------------------------------------------

    def performance(self) -> dict[str, object]:
        """The model's own track record. Published, per the spec, not implied."""
        rows = self._connection.execute(
            """
            SELECT direction, realized_r, created_at, closed_at
              FROM signals
             WHERE realized_r IS NOT NULL
             ORDER BY closed_at
            """
        ).fetchall()

        results = [dict(row) for row in rows]
        closed = len(results)
        open_count = self._connection.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE status IN (?,?)",
            (SignalStatus.OPEN.value, SignalStatus.TARGET1.value),
        ).fetchone()["n"]

        if closed == 0:
            return {
                "closed": 0, "open": open_count, "wins": 0, "losses": 0,
                "hit_rate": None, "expectancy_r": None, "total_r": 0.0,
                "max_drawdown_r": 0.0, "avg_hold_minutes": None, "by_direction": {},
                "note": "no closed signals yet -- the track record starts empty and stays honest",
            }

        r_values = [float(r["realized_r"]) for r in results]
        wins = sum(1 for r in r_values if r > 0)
        equity, peak, max_drawdown = 0.0, 0.0, 0.0
        for value in r_values:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        holds = []
        for row in results:
            created, closed_at = _parse(row["created_at"]), _parse(row["closed_at"])
            if created and closed_at:
                holds.append((closed_at - created).total_seconds() / 60.0)

        by_direction: dict[str, dict[str, object]] = {}
        for side in (Direction.LONG.value, Direction.SHORT.value):
            subset = [float(r["realized_r"]) for r in results if r["direction"] == side]
            if subset:
                by_direction[side] = {
                    "closed": len(subset),
                    "hit_rate": round(100.0 * sum(1 for v in subset if v > 0) / len(subset), 1),
                    "expectancy_r": round(sum(subset) / len(subset), 3),
                }

        return {
            "closed": closed,
            "open": open_count,
            "wins": wins,
            "losses": closed - wins,
            "hit_rate": round(100.0 * wins / closed, 1),
            "expectancy_r": round(sum(r_values) / closed, 3),
            "total_r": round(sum(r_values), 2),
            "max_drawdown_r": round(max_drawdown, 2),
            "avg_hold_minutes": round(sum(holds) / len(holds), 1) if holds else None,
            "by_direction": by_direction,
        }


def _row_to_signal(row: sqlite3.Row) -> Signal:
    drivers = tuple(
        Driver(label=d["label"], score=d["score"], weight=d["weight"], detail=d.get("detail", ""))
        for d in json.loads(row["drivers_json"])
    )
    levels = Levels(
        entry_low=row["entry_low"], entry_high=row["entry_high"], stop=row["stop"],
        target1=row["target1"], target2=row["target2"], hold_minutes=row["hold_minutes"],
    )
    created_at = _parse(row["created_at"])
    expires_at = _parse(row["expires_at"])
    assert created_at is not None and expires_at is not None  # NOT NULL in schema
    return Signal(
        id=row["id"], symbol=row["symbol"], base=row["base"],
        direction=Direction(row["direction"]),
        composite_score=row["composite_score"], confidence=row["confidence"],
        levels=levels, drivers=drivers,
        leg_scores=json.loads(row["leg_scores_json"]),
        reduced_confidence=bool(row["reduced_confidence"]),
        reduced_confidence_reason=row["reduced_confidence_reason"],
        reference_price=row["reference_price"], setup_score=row["setup_score"],
        created_at=created_at, expires_at=expires_at,
        status=SignalStatus(row["status"]),
        closed_at=_parse(row["closed_at"]), close_price=row["close_price"],
        close_reason=row["close_reason"], realized_r=row["realized_r"],
        peak_r=row["peak_r"], trough_r=row["trough_r"],
        target1_hit_at=_parse(row["target1_hit_at"]), notes=row["notes"],
    )
