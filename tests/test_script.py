"""Tests for producer/script.py — generate_episode_script() and related surface.

Coverage per docs/specs/2026-04-17-producer-step2-prompt.md §D4:
- Group B — system prompt structural assertions (string-in checks)
- Group C — validation assertions: drop-segments, first-segue-empty, short-script
- Group D — happy path (mocked generate_segment)
"""

from __future__ import annotations

import pytest

from agents.protocol import Brief, Pitch, TodayContext
from producer.script import (
    SYSTEM_PROMPT,
    EpisodeScript,
    SegmentScript,
    _strip_inline_markup,
    generate_episode_script,
    split_opener_inputs,
    stream_episode_script,
)


# ── Inline-markup stripping ──────────────────────────────────────────


class TestStripInlineMarkup:
    def test_strips_single_cite_tag_preserves_content(self):
        raw = 'Earlier this year, <cite index="7-21">Zhu took first prize</cite>.'
        assert _strip_inline_markup(raw) == "Earlier this year, Zhu took first prize."

    def test_strips_multiple_cite_tags_multi_index(self):
        raw = (
            '<cite index="4-1,4-2">Lim\'s Goldberg recording</cite> has dominated '
            'charts <cite index="2-5">worldwide</cite>.'
        )
        assert _strip_inline_markup(raw) == (
            "Lim's Goldberg recording has dominated charts worldwide."
        )

    def test_strips_br_tags(self):
        assert _strip_inline_markup("line one<br><br>line two") == "line one line two"
        assert _strip_inline_markup("a<br/>b<br />c") == "a b c"

    def test_cite_tag_spanning_newlines(self):
        raw = '<cite index="3-1">first line\nsecond line</cite> tail'
        assert _strip_inline_markup(raw) == "first line\nsecond line tail"

    def test_plain_text_unchanged(self):
        assert _strip_inline_markup("plain spoken words") == "plain spoken words"

    def test_collapses_double_spaces_from_removal(self):
        raw = "<cite>foo</cite>  bar"
        assert "  " not in _strip_inline_markup(raw)


# ── Fixtures ──────────────────────────────────────────────────────────


def _full_pitch(
    agent: str = "youtube",
    title: str = "Jazz exploration",
    priority: float = 0.91,
    suggested_length_sec: int = 90,
    claim_kind: str = "rising",
    provenance_shape: str = "balanced",
    thin_signal: bool = False,
    data: dict | None = None,
) -> dict:
    """Build a fully-populated Pitch with every field set."""
    return {
        "agent": agent,
        "title": title,
        "hook": "you've been getting into jazz lately",
        "source_refs": ["Blue Note Records", "Coltrane Live at Birdland"],
        "data": data if data is not None else {},
        "priority": priority,
        "claim_kind": claim_kind,
        "provenance_shape": provenance_shape,
        "thin_signal": thin_signal,
        "suggested_length_sec": suggested_length_sec,
    }


_TODAY: TodayContext = {
    "date": "2026-04-17",
    "day_of_week": "Thursday",
    "time_of_day": "morning",
    "weather_summary": "rainy, 12°C",
    "calendar_events": ["Team standup 10:00"],
}


_BRIEF: Brief = {"today_context": _TODAY}


def _seg(
    agent: str,
    title: str,
    segue_in: str = "",
    script: str = "Here's a substantial enough script body for the segment.",
    estimated_length_sec: int = 60,
) -> SegmentScript:
    return SegmentScript(
        agent=agent,
        pitch_title=title,
        segue_in=segue_in,
        script=script,
        estimated_length_sec=estimated_length_sec,
    )


# ── Group B: system prompt structural assertions ──────────────────────


class TestSystemPrompt:
    def test_has_claim_kind_directive_block(self):
        """All 4 claim_kind values + Permitted/Prohibited words appear."""
        for kind in ("durable", "rising", "discovery", "neutral"):
            assert kind in SYSTEM_PROMPT, f"missing claim_kind: {kind!r}"
        assert "Permitted" in SYSTEM_PROMPT
        assert "Prohibited" in SYSTEM_PROMPT

    def test_has_field_legend(self):
        """Every payload field appears in the legend.

        rationale, priority, suggested_length_sec, provenance_shape, and
        target_total_secs were removed from the Step-2 LLM payload
        (SYSTEM_PROMPT told the LLM to ignore them), so the legend no
        longer carries them either.
        """
        for field in (
            "hook", "source_refs", "data",
            "claim_kind", "thin_signal",
        ):
            assert field in SYSTEM_PROMPT, f"missing field in legend: {field!r}"
        for removed in (
            "rationale", "priority", "suggested_length_sec",
            "provenance_shape", "target_total_secs",
        ):
            assert f"`{removed}`" not in SYSTEM_PROMPT, (
                f"legend still carries removed field: {removed!r}"
            )

    def test_has_per_agent_data_crib(self):
        """Each agent appears in a data-crib context."""
        for agent in ("weather", "calendar", "youtube", "alices"):
            assert agent in SYSTEM_PROMPT, f"missing agent in crib: {agent!r}"
        assert "data.current" in SYSTEM_PROMPT
        assert "data.events" in SYSTEM_PROMPT
        assert "notable_facts" in SYSTEM_PROMPT

    def test_has_thin_signal_handling(self):
        """thin_signal handling block names per-agent nudge phrasings."""
        assert "thin_signal" in SYSTEM_PROMPT
        assert "more personal as your YouTube activity grows" in SYSTEM_PROMPT
        assert "Local forecast wasn't available today" in SYSTEM_PROMPT

    def test_has_hook_data_layering_rule(self):
        """Hook vs data layering rule key phrases present."""
        assert "phrasing ceiling" in SYSTEM_PROMPT
        assert "read-only context" in SYSTEM_PROMPT
        assert "content source" in SYSTEM_PROMPT

    def test_has_web_search_usage_block(self):
        """System prompt instructs the model on web_search tool usage + query rules."""
        assert "web_search" in SYSTEM_PROMPT
        # Query-derivation rule: title-only seed, no listener proper nouns.
        assert "title" in SYSTEM_PROMPT
        # Must forbid pulling source_refs (listener proper nouns) into the query.
        assert "source_refs" in SYSTEM_PROMPT
        # Fallback discipline: broaden once, then hook-narration.
        assert "broaden" in SYSTEM_PROMPT.lower()

    def test_has_narration_contract_block(self):
        """Narration contract beats: lead → factual body → flex band → takeaway."""
        prompt_lower = SYSTEM_PROMPT.lower()
        for beat in ("lead", "factual body", "flex band", "takeaway"):
            assert beat in prompt_lower, f"missing narration beat: {beat!r}"

    def test_has_source_recitation_rule(self):
        """Listener proper nouns NOT spoken inside the story body."""
        prompt_lower = SYSTEM_PROMPT.lower()
        # Explicit rule forbidding recitation of channel names / video titles / source_refs.
        assert "recit" in prompt_lower  # matches "recite" / "recitation"
        assert "source_refs" in SYSTEM_PROMPT

    def test_has_json_safety_rules(self):
        """Output schema section instructs the model on JSON escaping discipline.

        Invalid JSON breaks the pipeline; this is enforced at parse time too
        (see producer/script.py generate_segment repair path), but the prompt
        is the first line of defense. research_outcome is NOT in the output
        schema — observed `server_tool_use` blocks are the source of truth.
        """
        assert "JSON safety rules" in SYSTEM_PROMPT
        assert "\\\"" in SYSTEM_PROMPT
        assert "\\n" in SYSTEM_PROMPT
        # The LLM-self-report field is gone; search usage is observed, not claimed.
        assert "research_outcome" not in SYSTEM_PROMPT

    def test_encourages_source_refs_personalization(self):
        """Prompt now encourages referencing source_refs for personalization,
        while preserving claim_kind discipline on temporal framing.

        Earlier spec forbade explicit bridges; the post-2026-04-19 design
        reverses that — listener channel/video names are the substrate of
        real personalization and should be referenced where they sharpen
        the tie. claim_kind still bounds what claims the takeaway can make.
        """
        assert "Personalization via `source_refs`" in SYSTEM_PROMPT
        prompt_lower = SYSTEM_PROMPT.lower()
        assert "encouraged" in prompt_lower
        # claim_kind discipline preserved — no invented durability claims.
        assert "you've been into x" in prompt_lower


# ── Group C: validation assertions ────────────────────────────────────
# Tests call stream_episode_script directly (async layer where validation lives).


class TestValidation:
    @pytest.mark.asyncio
    async def test_first_segment_nonempty_segue_in_raises(self, monkeypatch):
        """First segment with non-empty segue_in raises ValueError."""
        pitches = [_full_pitch(agent="weather", title="Weather in SF")]

        async def fake_generate_segment(segment, brief, is_first):
            return _seg(
                agent="weather",
                title="Weather in SF",
                segue_in="And now, the weather...",  # should be empty for first segment
            )

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        with pytest.raises(ValueError, match=r"segue_in"):
            async for _ in stream_episode_script(pitches, _BRIEF):
                pass

    @pytest.mark.asyncio
    async def test_short_script_raises(self, monkeypatch):
        """Segment script shorter than 20 chars raises ValueError naming the segment."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]

        calls = [0]

        async def fake_generate_segment(segment, brief, is_first):
            calls[0] += 1
            if calls[0] == 1:
                return _seg(agent="weather", title="Weather in SF")
            return _seg(
                agent="youtube",
                title="Jazz exploration",
                segue_in="And here's some music.",
                script="Hi.",  # too short
            )

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        with pytest.raises(ValueError, match=r"Jazz exploration"):
            async for _ in stream_episode_script(pitches, _BRIEF):
                pass

    @pytest.mark.asyncio
    async def test_drops_segment_raises(self, monkeypatch):
        """Iterator whose output_keys miss an input key raises ValueError naming the agent."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]

        async def fake_generate_segment(segment, brief, is_first):
            # youtube segment returns a wrong title so output_keys won't match input_keys
            if segment["agent"] == "youtube":
                return _seg(agent="youtube", title="WRONG TITLE")
            return _seg(agent=segment["agent"], title=segment["title"])

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        with pytest.raises(ValueError, match=r"youtube"):
            async for _ in stream_episode_script(pitches, _BRIEF):
                pass


# ── Group D: happy path ───────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_well_formed_response_passes(self, monkeypatch):
        """A complete, valid 2-segment stream returns successfully with expected shape."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]

        segs = [
            _seg(
                agent="weather",
                title="Weather in SF",
                segue_in="",
                script="Currently 55F and rainy in San Francisco. Highs near 60 today.",
                estimated_length_sec=45,
            ),
            _seg(
                agent="youtube",
                title="Jazz exploration",
                segue_in="From the weather, let's pivot to something for your ears.",
                script="You've been getting into jazz lately — Coltrane Live at Birdland turned up in a recent like.",
                estimated_length_sec=90,
            ),
        ]
        idx = [0]

        async def fake_generate_segment(segment, brief, is_first):
            result = segs[idx[0]]
            idx[0] += 1
            return result

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        collected: list[SegmentScript] = []
        async for seg in stream_episode_script(pitches, _BRIEF):
            collected.append(seg)

        assert len(collected) == 2
        assert collected[0]["segue_in"] == ""
        assert collected[0]["agent"] == "weather"
        assert collected[1]["agent"] == "youtube"
        assert collected[1]["estimated_length_sec"] == 90


# ── Group E: opener-input split ───────────────────────────────────────


class TestSplitOpenerInputs:
    def test_splits_weather_calendar_from_content(self):
        weather = _full_pitch(agent="weather", title="Weather in SF")
        calendar = _full_pitch(agent="calendar", title="Today's schedule")
        youtube = _full_pitch(agent="youtube", title="Jazz")
        alices = _full_pitch(agent="alices", title="PG essay")
        w, c, content = split_opener_inputs([weather, calendar, youtube, alices])
        assert w is weather
        assert c is calendar
        assert content == [youtube, alices]

    def test_returns_none_when_opener_input_absent(self):
        youtube = _full_pitch(agent="youtube", title="Jazz")
        w, c, content = split_opener_inputs([youtube])
        assert w is None
        assert c is None
        assert content == [youtube]

    def test_preserves_order_within_content(self):
        alices = _full_pitch(agent="alices", title="PG")
        youtube = _full_pitch(agent="youtube", title="Jazz")
        _, _, content = split_opener_inputs([alices, youtube])
        assert content == [alices, youtube]

    def test_picks_first_when_multiple_weather_or_calendar_pitches(self):
        """Guaranteed-slots flow emits one per agent; but the split is defensive."""
        w1 = _full_pitch(agent="weather", title="w1")
        w2 = _full_pitch(agent="weather", title="w2")
        youtube = _full_pitch(agent="youtube", title="y")
        w, c, content = split_opener_inputs([w1, w2, youtube])
        assert w is w1
        assert c is None
        assert content == [youtube]


# ── Group F: generate_episode_script end-to-end routing ───────────────


class TestGenerateEpisodeScriptRouting:
    def test_splits_and_calls_opener_then_streams_content(self, monkeypatch):
        """generate_episode_script fuses weather+calendar into opener; streams the rest."""
        weather = _full_pitch(agent="weather", title="Weather in SF")
        calendar = _full_pitch(agent="calendar", title="Today's schedule")
        youtube = _full_pitch(agent="youtube", title="Jazz")
        alices = _full_pitch(agent="alices", title="PG essay")

        opener_call = {}
        stream_call = {}

        async def fake_opener(w, c, first, brief):
            opener_call["weather"] = w
            opener_call["calendar"] = c
            opener_call["first"] = first
            return "o" * 250

        async def fake_sign_off(brief):
            return "bye"

        async def fake_generate_segment(segment, brief, is_first):
            return _seg(agent=segment["agent"], title=segment["title"])

        monkeypatch.setattr("producer.script.generate_opener", fake_opener)
        monkeypatch.setattr("producer.script.generate_sign_off", fake_sign_off)
        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        def capture_stream(selected, brief):
            stream_call["selected"] = selected
            return stream_episode_script(selected, brief)

        monkeypatch.setattr("producer.script.stream_episode_script", capture_stream)

        episode = generate_episode_script(
            [weather, calendar, youtube, alices], _BRIEF
        )

        assert opener_call["weather"] is weather
        assert opener_call["calendar"] is calendar
        assert opener_call["first"] is youtube
        assert stream_call["selected"] == [youtube, alices]
        assert episode["opener"] == "o" * 250
        assert episode["sign_off"] == "bye"
        assert [s["agent"] for s in episode["segments"]] == ["youtube", "alices"]

    def test_raises_when_no_content_pitches(self, monkeypatch):
        weather = _full_pitch(agent="weather", title="w")
        calendar = _full_pitch(agent="calendar", title="c")
        with pytest.raises(ValueError, match="no content pitches"):
            generate_episode_script([weather, calendar], _BRIEF)
