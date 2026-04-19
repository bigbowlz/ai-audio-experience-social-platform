"""Opt-in end-to-end test: one real generate_segment() call against a real pitch.

Skipped unless RUN_LIVE_LLM=1. Writes its artifact under
tmp/test_outputs/segment_scripts/ via RADIO_CACHE_DIR=tmp/test_outputs/ so the
user can open the file after the run and audit the segue_in, script body,
research_outcome, and raw_llm_text the model produced.

Spec: docs/specs/2026-04-18-producer-news-narration-design.md §3 Test posture.

Run it:
    RUN_LIVE_LLM=1 pytest tests/test_segment_live.py -v -s

Artifact location after the run:
    tmp/test_outputs/segment_scripts/youtube_underwater_photography_YYYYMMDD_130.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.protocol import Brief, Pitch
from producer.script import _segment_cache_path, generate_segment


_RUN_LIVE = os.environ.get("RUN_LIVE_LLM") == "1"


@pytest.fixture(autouse=True)
def _guard_live(monkeypatch):
    """Force RADIO_CACHE_DIR=tmp/test_outputs/ for every test in this module.

    The directory is intentionally outside the default cache so real episode
    cache files don't get clobbered by test runs.
    """
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "tmp" / "test_outputs"
    monkeypatch.setenv("RADIO_CACHE_DIR", str(out_dir))
    yield


def _live_pitch() -> Pitch:
    return {
        "agent": "youtube",
        "title": "Underwater photography",
        "hook": (
            "You've been getting into underwater photography lately — "
            "National Geographic's been showing up in your subs more and more."
        ),
        "source_refs": ["National Geographic", "BBC Earth"],
        "data": {},
        "priority": 0.9,
        "thin_signal": False,
        "claim_kind": "rising",
        "provenance_shape": "balanced",
        "suggested_length_sec": 90,
    }


def _brief() -> Brief:
    return {"today_context": {
        "date": "2026-04-18",
        "day_of_week": "Saturday",
        "time_of_day": "morning",
        "weather_summary": None,
        "calendar_events": None,
    }}


@pytest.mark.live_llm
@pytest.mark.asyncio
@pytest.mark.skipif(not _RUN_LIVE, reason="Set RUN_LIVE_LLM=1 to run live LLM tests")
async def test_generate_segment_writes_inspectable_artifact(tmp_path):
    """Live end-to-end: real LLM + real web_search; writes an inspectable artifact.

    After the test passes, open the file at the asserted path and audit:
    - `segment.segue_in` is empty (is_first=True) or ≤6 words.
    - `segment.script` reads like a news story, not a listener-data restatement.
    - `debug.research_outcome` is "story" (broadened search fell back to "hook_fallback").
    - `debug.raw_llm_text` contains the model's raw JSON output.
    - Listener proper nouns ("National Geographic", "BBC Earth") do NOT appear
      in `segment.script` — the source-recitation rule must hold.
    """
    pitch = _live_pitch()
    brief = _brief()

    seg = await generate_segment(pitch, brief, is_first=True)

    assert seg["agent"] == "youtube"
    assert seg["pitch_title"] == "Underwater photography"
    assert seg["segue_in"] == ""
    assert len(seg["script"]) >= 100   # real narration, not a stub
    # Source-recitation invariant — listener proper nouns forbidden in the body.
    assert "National Geographic" not in seg["script"]
    assert "BBC Earth" not in seg["script"]

    # Artifact must exist and be inspectable.
    wpm = 130
    expected = _segment_cache_path(
        pitch["agent"], pitch["title"], brief["today_context"]["date"], wpm
    )
    assert expected.exists(), f"artifact missing at {expected}"
    art = json.loads(expected.read_text(encoding="utf-8"))
    assert art["segment"]["pitch_title"] == "Underwater photography"
    assert art["debug"]["research_outcome"] in ("story", "hook_fallback")
    assert "raw_llm_text" in art["debug"]
    assert art["debug"]["input_pitch"]["title"] == "Underwater photography"
    print(f"\nLIVE ARTIFACT: {expected}")
