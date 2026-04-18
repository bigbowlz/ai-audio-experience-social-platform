"""Shape tests for new typed shapes added in 2026-04-17 producer alignment.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Task 0.1
      producer/docs/DESIGN.md §Interface contract
"""
from __future__ import annotations

from agents.protocol import (
    CreatorAgentListing,
    ExternalDecision,
    Pitch,
    RunningOrder,
)


def test_running_order_minimal_shape():
    pitch: Pitch = {
        "agent": "youtube", "title": "t", "hook": "h", "rationale": "r",
        "source_refs": [], "priority": 0.9, "thin_signal": False,
        "claim_kind": "neutral", "provenance_shape": "balanced",
        "suggested_length_sec": 90,
    }
    order: RunningOrder = {
        "segments": [pitch],
        "total_sec": 90,
        "guaranteed_count": 1,
        "bonus_count": 0,
    }
    assert order["segments"][0]["agent"] == "youtube"
    assert order["total_sec"] == 90


def test_external_decision_invoke_shape():
    decision: ExternalDecision = {"decision": "invoke", "rationale": "v0 always-invoke"}
    assert decision["decision"] == "invoke"


def test_external_decision_skip_shape():
    decision: ExternalDecision = {"decision": "skip", "rationale": "test"}
    assert decision["decision"] == "skip"


def test_creator_agent_listing_shape():
    listing: CreatorAgentListing = {
        "handle": "@AlicesLens",
        "display_name": "Alice's Lens",
        "scope": "tech / startup culture",
        "price_usdc": 0.10,
        "wallet_address": "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2",
    }
    assert listing["price_usdc"] == 0.10
