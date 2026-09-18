"""The analysis legs.

Phase 1 ships `technical`. The fundamental/on-chain and news/sentiment legs
arrive in phases 2 and 3; fusion already renormalises over whichever legs
report, so adding them is a new module and a registration, not a reweighting.
"""

from .technical import score_technical

__all__ = ["score_technical"]
