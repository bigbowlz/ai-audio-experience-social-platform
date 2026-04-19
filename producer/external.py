"""Producer external-agent invocation flow (v0: always-invoke + hardcoded list).

Three pure functions:
  decide_external_invocation(pitches) -> ExternalDecision
  query_marketplace()                 -> list[CreatorAgentListing]
  select_external(candidates, brief)  -> CreatorAgentListing

Spec: producer/docs/DESIGN.md §Interface contract (lines 28-49)
      Master design §10 (Alice's agent invoked by Producer)
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 2 (decision 1d)
"""

from __future__ import annotations

from agents.protocol import (
    Brief,
    CreatorAgentListing,
    ExternalDecision,
    Pitch,
)


# v0: hardcoded list. v1: real marketplace query.
_MARKETPLACE_V0: list[CreatorAgentListing] = [
    {
        "handle": "@GoddamnAxl",
        "display_name": "External Lens",
        "scope": "tech / startup culture",
        "price_usdc": 0.10,
        "wallet_address": "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2",
    },
]


def decide_external_invocation(
    pitches_by_agent: dict[str, list[Pitch]],
) -> ExternalDecision:
    """v0: always invokes. Returns {decision: 'invoke', rationale: ...}.

    v1 may condition on topic-cluster entropy / cocoon detection per
    producer/docs/DESIGN.md interface contract.
    """
    return ExternalDecision(
        decision="invoke",
        rationale="v0 anti-cocoon policy: always bring an outside voice",
    )


def query_marketplace() -> list[CreatorAgentListing]:
    """v0: returns the hardcoded marketplace list."""
    return list(_MARKETPLACE_V0)


def select_external(
    candidates: list[CreatorAgentListing],
    brief: Brief,
) -> CreatorAgentListing:
    """v0: returns @GoddamnAxl (the only listing that matches seed topics).

    Brief is accepted for v1 forward-compat; v0 ignores it.
    """
    _ = brief
    if not candidates:
        raise ValueError("query_marketplace returned no candidates")
    for c in candidates:
        if c["handle"] == "@GoddamnAxl":
            return c
    return candidates[0]
