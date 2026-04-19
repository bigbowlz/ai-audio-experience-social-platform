"""LLM hook generation for YouTubeAgent pitch() — Layer 3.

Replaces the Layer 2 template hooks with a constrained Claude call.
The LLM selects 3–5 candidates from the algo-assembled bundle and
writes hooks governed by claim_kind + provenance_shape.

Spec: agents/docs/prompt_design.md §1–§2
      agents/youtube/docs/DESIGN.md §pitch() flow
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

from agents.protocol import Brief, Pitch
from agents.youtube.extractor import Contributor
from agents.youtube.guardrails import ClaimKind, ProvenanceShape

# ── Constants ────────────────────────────────────────────────────────

MODEL = os.environ.get("YOUTUBE_LLM_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048

# ── System prompts ───────────────────────────────────────────────────
#
# Two prompts share the same candidate-bundle format but differ in framing:
#
#   YOUTUBE_SYSTEM_PROMPT — for agent_name="youtube". Prose hooks in
#     second-person ("you've been into X"). The listener's own data.
#
#   PATRICKS_SYSTEM_PROMPT — for agent_name="alices". Structured
#     what/source/goal hooks that explicitly flag external-curator
#     provenance. The pitch reflects Alice's taste, not the listener's —
#     the Producer must narrate accordingly.

YOUTUBE_SYSTEM_PROMPT = """\
You are a radio show research assistant. Your job is to select the best \
3–5 topics from a ranked candidate list and write a short "hook" for each — \
a creative brief that a Producer will use to script a radio segment. \
Hooks are NOT spoken on-air; they are input for the Producer.

## Rules

1. **Select 3–5 candidates** from the provided list. Prefer variety over \
   clustering similar topics. You may reorder by narrative flow.
2. **Assign priority ∈ [0, 1]** to each selected topic. Higher = more \
   important. Use the algo `score` as a baseline but adjust for narrative \
   interest and variety.
3. **Write a hook** (1–3 sentences) for each selected topic. The hook must \
   conform to the `claim_kind` and `provenance_shape` constraints below.
4. **Never invent facts.** Every claim in a hook must be traceable to the \
   provenance entries provided. Do not hallucinate channel names, video \
   titles, dates, or statistics.
5. **Never reference topics you did not select.** Each hook is self-contained.

## claim_kind constraints

Each candidate has a `claim_kind` that governs temporal framing:

- **durable**: Permitted: "you've been into X", "a longtime favorite", \
  reference subscription dates. Prohibited: "lately", "recently", "getting into".
- **rising**: Permitted: "you've been getting into X lately", \
  "X is taking over your feed". Prohibited: "longtime", "always been".
- **discovery**: Permitted: "you've been exploring X", \
  "some X caught your eye recently". Prohibited: "deep into", "longtime", "always".
- **neutral**: Permitted: factual — "X showed up in your [subs/likes]", \
  reference specific channel/video names. Prohibited: any temporal or intensity claim.

## provenance_shape constraints

Each candidate has a `provenance_shape` that governs evidence framing:

- **balanced**: Both subscription and recent-like evidence exist. You may \
  reference both durable interest (subscription dates) and recent activity \
  (liked videos).
- **sub_only**: Only subscriptions. Frame as established interest. Do NOT \
  claim recent activity or trending behavior.
- **like_only**: Only recent likes. Frame as discovery or exploration. Do NOT \
  claim longstanding interest or deep familiarity.

## Output format

Return a JSON array of objects. Each object has:
- `topic`: the topic key (must match a candidate's `topic` field exactly)
- `title`: a short human-readable title for the segment (2–5 words)
- `hook`: the creative brief (1–3 sentences)
- `priority`: float in [0, 1]

Return ONLY the JSON array — no markdown fences, no commentary.
"""

PATRICKS_SYSTEM_PROMPT = """\
You are a research assistant writing creative briefs for a radio Producer. \
The candidates below come from @AlicesLens — an EXTERNAL CURATOR whose \
YouTube activity was captured once (Day-0 data). These pitches reflect \
PATRICK'S taste, not the listener's.

Your job is to select 3–5 topics and write a structured hook for each. \
Hooks are NOT spoken on-air; they are input for the Producer.

## Rules

1. **Select 3–5 candidates** from the provided list. Prefer variety.
2. **Assign priority ∈ [0, 1]** per selected topic. Use algo `score` as \
   baseline, adjust for narrative variety.
3. **External-curator framing is mandatory.** Every hook must make it \
   unambiguous that the evidence comes from Alice's own account, not \
   the listener's. Use "Alice", never "you".
4. **Never invent facts.** Every factual claim must map to a provenance \
   entry (channel_name, video_title, subscribed_at, liked_at).
5. **Respect claim_kind for temporal framing** (see below).

## claim_kind directives (for Alice, not the listener)

- **durable**: "Alice has been into X for a while", reference \
  subscription dates. Prohibited: "lately", "recently".
- **rising**: "Alice has been getting into X lately", "X is trending \
  in his recent activity". Prohibited: "longtime", "always".
- **discovery**: "Alice recently surfaced X", "some X caught Alice's \
  eye". Prohibited: "deep into", "longtime".
- **neutral**: factual — "X appeared in Alice's subs/likes", reference \
  specific channel/video names. Prohibited: any temporal or intensity claim.

## Output hook format (structured, not prose)

Each hook is a multi-line string with three labeled sections:

```
WHAT: Curator recommendation on {topic} (claim_kind={claim_kind}) — {specific evidence from provenance, e.g. "@pg's essay on founders in Alice's recent likes"}.
SOURCE: @AlicesLens (external curator, pre-captured Day-0 data) — NOT the listener's own interest.
GOAL: Expose the listener to Alice's taste. Narrate as curator pick ('Alice's been into X', 'Alice flagged Y'), never as listener taste ('you've been into X'). Respect claim_kind directives for temporal framing.
```

The WHAT line varies per pitch — cite one or two concrete provenance \
entries. The SOURCE and GOAL lines are fixed text; copy them verbatim. \
Do not paraphrase SOURCE or GOAL.

## Output format

Return a JSON array of objects. Each object has:
- `topic`: the topic key (must match a candidate's `topic` field exactly)
- `title`: a short human-readable title for the segment (2–5 words)
- `hook`: the structured hook string (WHAT/SOURCE/GOAL, as above)
- `priority`: float in [0, 1]

Return ONLY the JSON array — no markdown fences, no commentary.
"""


def _system_prompt_for(agent_name: str) -> str:
    if agent_name == "alices":
        return PATRICKS_SYSTEM_PROMPT
    return YOUTUBE_SYSTEM_PROMPT


# Back-compat alias: some callers/tests may reference SYSTEM_PROMPT directly.
SYSTEM_PROMPT = YOUTUBE_SYSTEM_PROMPT


# ── Candidate formatting ─────────────────────────────────────────────

def _format_contributor(c: Contributor) -> dict[str, Any]:
    """Slim contributor for prompt context."""
    out: dict[str, Any] = {"kind": c["kind"], "channel": c["channel_name"]}
    if c["kind"] == "sub" and c["subscribed_at"]:
        out["subscribed_at"] = c["subscribed_at"][:10]  # date only
    if c["kind"] == "like":
        if c["video_title"]:
            out["video_title"] = c["video_title"]
        if c["liked_at"]:
            out["liked_at"] = c["liked_at"][:10]
    return out


def _format_bundle(
    bundle: list[dict[str, Any]],
    brief: Brief,
) -> str:
    """Format the candidate bundle + brief as the user message."""
    candidates = []
    for item in bundle:
        candidates.append({
            "topic": item["topic"],
            "score": round(item["score"], 4),
            "long_term": round(item["long_term"], 4),
            "recent": round(item["recent"], 4),
            "claim_kind": item["claim_kind"].value if isinstance(item["claim_kind"], ClaimKind) else item["claim_kind"],
            "provenance_shape": item["provenance_shape"].value if isinstance(item["provenance_shape"], ProvenanceShape) else item["provenance_shape"],
            "provenance": [_format_contributor(c) for c in item["provenance"]],
        })

    payload = {
        "candidates": candidates,
        "today_context": brief["today_context"],
    }
    return json.dumps(payload, indent=2)


# ── LLM call ─────────────────────────────────────────────────────────

def generate_pitches(
    bundle: list[dict[str, Any]],
    brief: Brief,
    agent_name: str = "youtube",
) -> list[Pitch]:
    """Call Claude to select 3–5 candidates and write constrained hooks.

    Falls back to template hooks if the LLM call fails or returns
    unparseable output.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")

    client = anthropic.Anthropic()
    user_msg = _format_bundle(bundle, brief)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_system_prompt_for(agent_name),
        messages=[{"role": "user", "content": user_msg}],
        timeout=30.0,
    )

    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    raw = response.content[0].text.strip()
    # Strip markdown fences if the model wraps anyway
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    selections = json.loads(raw)

    # Build a lookup for bundle items by topic
    bundle_by_topic = {item["topic"]: item for item in bundle}

    pitches: list[Pitch] = []
    for sel in selections:
        topic = sel["topic"]
        if topic not in bundle_by_topic:
            continue  # LLM selected a topic not in the bundle — skip

        item = bundle_by_topic[topic]
        contributors: list[Contributor] = item["provenance"]
        claim_kind = item["claim_kind"]
        prov_shape = item["provenance_shape"]

        # source_refs from provenance (human-readable names for downstream LLM consumption)
        source_refs = []
        for c in contributors:
            if c["kind"] == "sub":
                source_refs.append(c["channel_name"])
            elif c["video_title"]:
                source_refs.append(c["video_title"])
            else:
                source_refs.append(c["channel_name"])

        pitches.append(Pitch(
            agent=agent_name,
            title=sel.get("title", topic.replace("-", " ").title()),
            hook=sel["hook"],
            source_refs=list(dict.fromkeys(source_refs)),
            priority=min(1.0, max(0.0, float(sel.get("priority", item["score"])))),
            thin_signal=False,
            claim_kind=claim_kind.value if isinstance(claim_kind, ClaimKind) else claim_kind,
            provenance_shape=prov_shape.value if isinstance(prov_shape, ProvenanceShape) else prov_shape,
        ))

    return pitches
