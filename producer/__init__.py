"""Producer component — running-order assembly + script generation.

Public constants are exported here so individual modules don't drift.
See producer/docs/DESIGN.md for component-level contract.
"""

DEFAULT_LLM_MODEL = "claude-sonnet-4-6"
"""Anthropic model used by Step 1.5 (bonus selection) and Step 2 (script generation).

Bumped from the pre-2026-04-17 default (claude-sonnet-4-20250514) per decision
6.1a in the producer alignment cross-check. Override via PRODUCER_LLM_MODEL env var.
"""
