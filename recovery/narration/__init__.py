"""LLM surfaces — the only places a model touches this system.

    messages.py   customer recovery messages, English and Hinglish
    narrator.py   ledger rows -> a readable account of one case

Neither is on the money path. The retry/stop decision is a deterministic EV
calculation in `recovery/decision/`; these two turn its results into words.
Both fall back to written templates when the model is unavailable, and both
verify the model's output before using it — a fluent sentence with a wrong
number in it is worse than a plain one with the right number.
"""

from recovery.narration.messages import (
    Channel,
    Language,
    MessageWriter,
    looks_like_hinglish,
)
from recovery.narration.narrator import Narrator, facts, render

__all__ = [
    "Channel",
    "Language",
    "MessageWriter",
    "Narrator",
    "facts",
    "looks_like_hinglish",
    "render",
]
