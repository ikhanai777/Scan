"""The analysis legs.

All three the spec calls for are here. Each reports -100..+100 with a reason
per component, and each declares itself unavailable rather than voting neutral
when it has too little to say. Fusion renormalises over whichever legs report,
so a key you do not have costs accuracy, never correctness.
"""

from .fundamental import score_fundamental
from .sentiment import score_sentiment
from .technical import score_technical

__all__ = ["score_technical", "score_fundamental", "score_sentiment"]
