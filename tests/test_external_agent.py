"""Tests for ExternalAgent — the external creator agent.

Uses the pre-captured Day-0 JSON as fixture (same data also proves the
shared extractor contract works from a second caller).

Spec: agents/youtube/docs/DESIGN.md §Shared extractor
      agents/docs/DESIGN.md §Interface contract
"""

from __future__ import annotations

import os

import pytest


# ── Agent metadata ──


class TestAgentMetadata:
    """ExternalAgent has correct DataAgent protocol fields."""

    def test_name(self):
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        assert agent.name == "external"

    def test_display_name_is_curator_handle(self):
        from agents.external.agent import CURATOR_HANDLE, ExternalAgent

        agent = ExternalAgent()
        assert agent.display_name == CURATOR_HANDLE

    def test_external(self):
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        assert agent.external is True

    def test_price_usdc(self):
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        assert agent.price_usdc == 0.10

    def test_wallet_address_set(self):
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        assert agent.wallet_address is not None
        assert agent.wallet_address.startswith("0x")

    def test_satisfies_data_agent_protocol(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import DataAgent

        agent = ExternalAgent()
        assert isinstance(agent, DataAgent)


# ── fetch_context ──


class TestFetchContext:
    """fetch_context loads curator data and returns ScopeContext with InterestProfile."""

    def test_returns_profile(self):
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        assert "profile" in ctx
        profile = ctx["profile"]
        assert "combined_topic_scores" in profile
        assert "topic_provenance" in profile
        assert "long_term_topic_scores" in profile
        assert "recent_topic_scores" in profile
        assert "computed_at" in profile
        assert "stats" in profile

    def test_profile_has_topics(self):
        """Curator data has plenty of subs + likes — profile should have topics."""
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        profile = ctx["profile"]
        assert len(profile["combined_topic_scores"]) > 0
        assert len(profile["topic_provenance"]) > 0

    def test_profile_scores_l1_normalized(self):
        from agents.external.agent import ExternalAgent

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        profile = ctx["profile"]
        total = sum(profile["combined_topic_scores"].values())
        assert abs(total - 1.0) < 0.01 or total == 0.0


# ── pitch ──


def _make_brief() -> dict:
    return {
        "today_context": {
            "date": "2026-04-16",
            "day_of_week": "Wednesday",
            "time_of_day": "morning",
            "weather_summary": "clear, 22°C",
            "calendar_events": None,
        }
    }


class TestPitch:
    """pitch() returns 3–5 Pitch objects with agent='external'."""

    def test_returns_3_to_5_pitches(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        assert 3 <= len(pitches) <= 5

    def test_all_pitches_have_agent_external(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        for p in pitches:
            assert p["agent"] == "external"

    def test_pitch_fields_complete(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        for p in pitches:
            assert "title" in p
            assert "hook" in p
            assert "source_refs" in p
            assert "priority" in p
            assert "thin_signal" in p
            assert "claim_kind" in p
            assert "provenance_shape" in p

    def test_priority_in_range(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        for p in pitches:
            assert 0.0 <= p["priority"] <= 1.0

    def test_valid_claim_kind(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        valid = {"durable", "rising", "discovery", "neutral"}
        for p in pitches:
            assert p["claim_kind"] in valid

    def test_valid_provenance_shape(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        valid = {"balanced", "sub_only", "like_only"}
        for p in pitches:
            assert p["provenance_shape"] in valid

    def test_not_thin_signal(self):
        """Curator has plenty of data — should never be thin-signal."""
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        for p in pitches:
            assert p["thin_signal"] is False

    def test_source_refs_are_strings(self):
        from agents.external.agent import ExternalAgent
        from agents.protocol import bootstrap_memory

        agent = ExternalAgent()
        ctx = agent.fetch_context("user1")
        pitches = agent.pitch(_make_brief(), bootstrap_memory(), ctx, "user1")
        for p in pitches:
            assert isinstance(p["source_refs"], list)
            for ref in p["source_refs"]:
                assert isinstance(ref, str)


# ── LLM agent_name passthrough ──


class TestLlmAgentName:
    """LLM module accepts agent_name parameter so external pitches get agent='external'."""

    def test_generate_pitches_accepts_agent_name(self):
        """generate_pitches should accept agent_name kwarg."""
        import inspect
        from agents.youtube.llm import generate_pitches

        sig = inspect.signature(generate_pitches)
        assert "agent_name" in sig.parameters
