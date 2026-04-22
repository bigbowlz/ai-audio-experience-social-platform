"""Prompt-rendering identities for the external curator agent.

v0: hardcoded constants. v1: per-user config sourced from marketplace
metadata and listener profile.

Kept separate from agent.py to avoid circular imports — agents/youtube/prompts.py
needs CURATOR_NAME / CURATOR_HANDLE, but agents/external/agent.py depends on
agents/youtube via the shared extractor, so the identity constants must live
in a dependency-free module.
"""

from __future__ import annotations


CURATOR_NAME = "Alice"
CURATOR_HANDLE = "@GoddamnAxl"
LISTENER_HANDLE = "@wanli"
