"""Record real market data to disk, and replay it in tests.

The test suite drives the scoring engine against deterministic synthetic price
series, because an assertion like "an uptrend scores long" needs a path whose
answer is known in advance, and no live market provides one. That is normal and
it is not fabrication: nothing synthetic reaches a database, a signal card, the
dashboard or the backtest.

But synthetic series are also *too clean*. Real markets have gaps, halted bars,
repeated closes, volume spikes at the open and stretches where nothing moves.
`cryptosignal record` captures real OHLCV from the venue into `fixtures/`, and
`load_fixtures()` makes the suite run the whole scoring path over it.

The contract for those tests is deliberately different from the synthetic ones.
Against a recorded market nobody knows the right answer, so they assert
**invariants** rather than outcomes: scores stay in range, no NaN escapes, no
component silently disappears, levels stay ordered, and the engine never
crashes on a shape a real venue produced. Those are the failures a synthetic
series never surfaces.

Recorded files carry the venue, symbol, timeframe and capture time, so a stale
fixture can be spotted rather than trusted forever.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import Candles

log = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FORMAT_VERSION = 1


@dataclass(frozen=True)
class Fixture:
    exchange: str
    symbol: str
    timeframe: str
    captured_at: datetime
    candles: Candles

    @property
    def age_days(self) -> float:
        return (datetime.now(UTC) - self.captured_at).total_seconds() / 86400.0

    @property
    def slug(self) -> str:
        return f"{self.exchange}-{self.symbol.replace('/', '-')}-{self.timeframe}"


def save(fixture: Fixture, directory: Path | None = None) -> Path:
    """Write one recording. Rows are the venue's own, unmodified."""
    directory = directory or FIXTURE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fixture.slug}.json"

    candles = fixture.candles
    payload = {
        "format": FORMAT_VERSION,
        "exchange": fixture.exchange,
        "symbol": fixture.symbol,
        "timeframe": fixture.timeframe,
        "captured_at": fixture.captured_at.isoformat(),
        "bars": len(candles),
        "note": "Real OHLCV as returned by the venue. Not modified, not generated.",
        "ohlcv": [
            [candles.timestamps[i], candles.open[i], candles.high[i],
             candles.low[i], candles.close[i], candles.volume[i]]
            for i in range(len(candles))
        ],
    }
    path.write_text(json.dumps(payload, separators=(",", ":")))
    log.info("recorded %d bars of %s to %s", len(candles), fixture.symbol, path)
    return path


def load(path: Path) -> Fixture | None:
    """Read one recording, or None if the file is not a usable fixture."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("fixture %s is unreadable: %s", path, exc)
        return None
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_VERSION:
        log.warning("fixture %s is not format %d", path, FORMAT_VERSION)
        return None

    rows = payload.get("ohlcv")
    if not isinstance(rows, list) or not rows:
        return None

    symbol = str(payload.get("symbol", "UNKNOWN/USDT"))
    timeframe = str(payload.get("timeframe", "15m"))
    candles = Candles.from_rows(symbol, timeframe, rows)
    if len(candles) == 0:
        return None

    try:
        captured = datetime.fromisoformat(str(payload.get("captured_at")))
    except ValueError:
        captured = datetime.now(UTC)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)

    return Fixture(
        exchange=str(payload.get("exchange", "unknown")),
        symbol=symbol, timeframe=timeframe, captured_at=captured, candles=candles,
    )


def load_fixtures(directory: Path | None = None) -> list[Fixture]:
    """Every recording on disk. Empty when none have been captured yet."""
    directory = directory or FIXTURE_DIR
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        fixture = load(path)
        if fixture is not None:
            out.append(fixture)
    return out


def record(feed, symbols: list[str], bars: int, exchange_id: str,
           directory: Path | None = None) -> list[Path]:
    """Pull real history for each symbol and write it out."""
    written = []
    for symbol in symbols:
        candles = feed.history(symbol, bars)
        if candles is None or len(candles) == 0:
            log.warning("no history returned for %s -- skipping", symbol)
            continue
        fixture = Fixture(
            exchange=exchange_id, symbol=symbol, timeframe=candles.timeframe,
            captured_at=datetime.now(UTC), candles=candles,
        )
        written.append(save(fixture, directory))
    return written
