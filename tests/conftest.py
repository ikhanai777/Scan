"""Fixtures. The synthetic market builders live in `support.py`."""

from __future__ import annotations

import pytest

from cryptosignal.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Defaults, but with the universe and DB pinned for deterministic tests."""
    return Settings(
        universe_size=20,
        shortlist_size=5,
        min_quote_volume_24h=1_000_000.0,
        max_spread_bps=10.0,
        database_path=":memory:",
        max_open_signals=5,
    )
