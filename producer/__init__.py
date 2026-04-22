"""Producer component — running-order assembly + script generation.

Public constants are exported here so individual modules don't drift.
See producer/docs/DESIGN.md for component-level contract.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_LLM_MODEL = "claude-sonnet-4-6"
"""Anthropic model used by Step 1.5 (bonus selection) and Step 2 (script generation).

Bumped from the pre-2026-04-17 default (claude-sonnet-4-20250514) per decision
6.1a in the producer alignment cross-check. Override via PRODUCER_LLM_MODEL env var.
"""

DEFAULT_WORDS_PER_MIN = 150
"""Spoken-word pacing target. Drives both length estimation (words → seconds)
and the per-segment target_words hint passed into the Producer LLM prompt.

130 wpm is a warm-conversational rate — faster than NPR-slow (~120) and
slower than podcast-energetic (~150). Override via PRODUCER_WORDS_PER_MIN
env var (int ≥ 1).
"""


def words_per_min() -> int:
    """Resolve the effective words-per-minute pacing.

    Reads PRODUCER_WORDS_PER_MIN on every call (no import-time capture) so tests
    can monkeypatch the env without reloading the module. Falls back to
    DEFAULT_WORDS_PER_MIN on absent / unparseable / non-positive values.
    """
    raw = os.environ.get("PRODUCER_WORDS_PER_MIN")
    if not raw:
        return DEFAULT_WORDS_PER_MIN
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WORDS_PER_MIN
    return value if value >= 1 else DEFAULT_WORDS_PER_MIN


DEFAULT_CACHE_DIR = Path("tmp/segment_script_cache")
"""Per-segment script artifact directory — one pretty-printed JSON file per
segment LLM call. Same-day hits return `artifact.segment` verbatim without
calling the LLM. Override via RADIO_CACHE_DIR env var (relative or absolute
path). See producer/docs/DESIGN.md and
docs/specs/2026-04-18-producer-news-narration-design.md §3.
"""


def cache_dir() -> Path:
    """Resolve the effective segment-script cache directory.

    Reads RADIO_CACHE_DIR on every call (no import-time capture) so tests can
    point at tmp/test_outputs/ via monkeypatch without reloading the module.
    Falls back to DEFAULT_CACHE_DIR on absent / empty values.
    """
    raw = os.environ.get("RADIO_CACHE_DIR")
    if not raw:
        return DEFAULT_CACHE_DIR
    return Path(raw)
