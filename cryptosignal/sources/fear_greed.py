"""The Crypto Fear & Greed index, from alternative.me. Free, keyless.

This is the spec's macro-regime filter: a 0-100 reading of how the whole market
is positioned, published daily. It is not a per-coin signal and this module does
not pretend it is -- every coin in a cycle gets the same regime reading, which
is exactly what a regime filter should be.

Read contrarian at the extremes, like funding. Extreme greed is where tops get
made and extreme fear is where bottoms do; the middle says nothing much. A
value of 50 is genuinely neutral, and that is a real reading rather than an
abstention -- the difference matters to the leg above.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from . import HTTPSource

FNG_URL = "https://api.alternative.me/fng/"
CACHE_SECONDS = 1800.0
#: Beyond these the crowd is one-sided enough to fade.
EXTREME_FEAR = 25.0
EXTREME_GREED = 75.0


@dataclass(frozen=True)
class RegimeReading:
    value: float                     # 0 = extreme fear, 100 = extreme greed
    label: str
    published_at: datetime | None
    previous: float | None           # yesterday's value, when the API returns it

    @property
    def change(self) -> float | None:
        return None if self.previous is None else self.value - self.previous


class FearGreedSource(HTTPSource):
    name = "fear_greed"

    def read(self) -> RegimeReading | None:
        cached = self.cached("fng")
        if cached is not None:
            return cached

        payload = self.get_json(FNG_URL, params={"limit": 2, "format": "json"})
        if not isinstance(payload, dict):
            return None
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            return None

        current = _row(rows[0])
        if current is None:
            return None
        value, label, published = current
        previous = None
        if len(rows) > 1:
            older = _row(rows[1])
            if older is not None:
                previous = older[0]

        reading = RegimeReading(value, label, published, previous)
        self.store("fng", reading, CACHE_SECONDS)
        return reading


def _row(entry) -> tuple[float, str, datetime | None] | None:
    if not isinstance(entry, dict):
        return None
    try:
        value = float(entry.get("value"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0 <= value <= 100:
        return None

    published = None
    raw_timestamp = entry.get("timestamp")
    try:
        published = datetime.fromtimestamp(int(raw_timestamp), tz=UTC)
    except (TypeError, ValueError, OSError):
        published = None

    return value, str(entry.get("value_classification") or "").strip(), published
