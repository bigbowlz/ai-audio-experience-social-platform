"""Tests for producer/external.py — external-agent invocation flow.

Spec: producer/docs/DESIGN.md §Interface contract
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 2
"""
from __future__ import annotations

from producer.external import (
    decide_external_invocation,
    query_marketplace,
    select_external,
)


def test_decide_external_invocation_v0_always_invokes():
    pitches = {"youtube": [{"agent": "youtube", "title": "t", "priority": 0.9}]}
    decision = decide_external_invocation(pitches)  # type: ignore[arg-type]
    assert decision["decision"] == "invoke"
    assert decision["rationale"]


def test_query_marketplace_returns_hardcoded_listings():
    listings = query_marketplace()
    assert len(listings) >= 1
    handles = {l["handle"] for l in listings}
    assert "@AlicesLens" in handles


def test_select_external_returns_alices_in_v0():
    listings = query_marketplace()
    chosen = select_external(listings, brief={
        "today_context": {
            "date": "2026-04-17", "day_of_week": "Thursday",
            "time_of_day": "morning", "weather_summary": None,
            "calendar_events": None,
        }
    })
    assert chosen["handle"] == "@AlicesLens"
    assert chosen["price_usdc"] == 0.10
