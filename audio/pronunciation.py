"""Post-LLM pronunciation preprocessing.

Regex rules applied BEFORE sending text to ElevenLabs.
The list grows during rehearsal listen-throughs.

Spec: audio/docs/DESIGN.md §SSML / Pronunciation handling
"""

from __future__ import annotations

import re

# (pattern, replacement) — applied in order.
# Patterns use word boundaries or lookahead to avoid false positives.
PRONUNCIATION_RULES: list[tuple[str, str]] = [
    (r"(?<!\w)@(\w+)", r"\1"),       # strip @ from handles (not emails)
    (r"\bCPI\b", "C P I"),
    (r"\bGDP\b", "G D P"),
    (r"\bAI\b", "A I"),
]


def apply_pronunciation(text: str) -> str:
    """Apply all pronunciation rules to text.

    Called on every segment's script text before TTS synthesis,
    regardless of voice or speaker.
    """
    for pattern, replacement in PRONUNCIATION_RULES:
        text = re.sub(pattern, replacement, text)
    return text
