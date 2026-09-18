"""Synthetic market builders.

Every test that touches scoring needs candles with a known shape -- a clean
uptrend, a flat range, a breakout -- so the assertion can be about the score's
direction rather than about whatever the market happened to do. Seeds are
fixed, so a failure is always reproducible.
"""

from __future__ import annotations

import time

import numpy as np

from cryptosignal.models import Candles, MarketSnapshot

BAR_MS = 15 * 60 * 1000


def make_candles(
    closes,
    symbol: str = "TEST/USDT",
    timeframe: str = "15m",
    volumes=None,
    wick: float = 0.004,
    end_ms: int | None = None,
) -> Candles:
    """Wrap a close series into OHLCV with plausible wicks and volume."""
    closes = np.asarray(closes, dtype=float)
    count = closes.size
    opens = np.concatenate([[closes[0]], closes[:-1]])
    spread = closes * wick
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    if volumes is None:
        volumes = np.full(count, 1000.0)
    volumes = np.asarray(volumes, dtype=float)
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    timestamps = [end - (count - 1 - i) * BAR_MS for i in range(count)]
    rows = [[timestamps[i], opens[i], highs[i], lows[i], closes[i], volumes[i]] for i in range(count)]
    return Candles.from_rows(symbol, timeframe, rows)


def trend_closes(count: int = 260, start: float = 100.0, drift: float = 0.004,
                 noise: float = 0.0012, seed: int = 7) -> np.ndarray:
    """A steady trend with small noise. Positive drift rises, negative falls."""
    rng = np.random.default_rng(seed)
    steps = drift + rng.normal(0.0, noise, count)
    return start * np.cumprod(1.0 + steps)


def range_closes(count: int = 260, centre: float = 100.0, amplitude: float = 0.01,
                 period: int = 24, seed: int = 11) -> np.ndarray:
    """A sideways oscillation: no trend for the leg to find."""
    rng = np.random.default_rng(seed)
    x = np.arange(count)
    wave = np.sin(2 * np.pi * x / period) * amplitude
    return centre * (1.0 + wave + rng.normal(0.0, 0.0006, count))


def breakout_closes(count: int = 260, base: float = 100.0, seed: int = 13,
                    break_bars: int = 4) -> np.ndarray:
    """A long quiet range, then a decisive break upward on the final bars.

    `break_bars` stays inside the feature layer's freshness window on purpose:
    a break that ran twenty bars ago is no longer a trigger, and the detector
    is meant to say so.
    """
    quiet = range_closes(count - break_bars, centre=base, amplitude=0.006, seed=seed)
    top = float(quiet.max())
    break_leg = top * np.cumprod(1.0 + np.full(break_bars, 0.012))
    return np.concatenate([quiet, break_leg])


def snapshot(symbol: str = "TEST/USDT", base: str = "TEST", last: float = 100.0,
             volume: float = 50_000_000.0, spread_bps: float = 2.0,
             quote: str = "USDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol, base=base, quote=quote, last=last,
        quote_volume_24h=volume, spread_bps=spread_bps, change_24h_pct=1.5,
    )
