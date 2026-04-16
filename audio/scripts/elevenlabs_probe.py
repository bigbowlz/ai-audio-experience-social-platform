"""ElevenLabs API probe — audio component Phase 0.

Spec: ~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/
      wanlizhou-main-design-20260415-233819.md  §Phase 0

Runs 7 probe objectives in order:
  1. Batch latency          p50/p95 over 5 runs (PRIMARY GATE: p50 < 10s)
  2. Streaming latency      time-to-first-chunk + full-file, 5 runs (informational)
  3. Model comparison       both objectives on turbo_v2_5 AND flash_v2_5
  4. Voice audition         3-4 narrator candidates, saves MP3s
  5. Output format          mp3_44100_128 vs mp3_22050_32 comparison
  6. Concurrent batch       4 parallel requests (GATE: no throttling)
  7. Pay-as-you-go check    voice count + rate-limit surfacing

Usage:
    ELEVEN_API_KEY=sk_... python audio/scripts/elevenlabs_probe.py
    python audio/scripts/elevenlabs_probe.py --api-key sk_...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from elevenlabs.client import ElevenLabs

# ── Sample text (design doc §Phase 0) ────────────────────────────────────────

SAMPLE_TEXT = (
    "You've been on a jazz deep dive lately. Three channels you follow have been dropping "
    "new content, and your recent likes are full of modal jazz and neo-soul crossovers. "
    "Anjunadeep, a channel you've followed since 2019, just released a live session that "
    "blends electronic textures with acoustic jazz instrumentation. Meanwhile, Adam Neely "
    "posted a theory breakdown of Kamasi Washington's latest album that's been sitting in "
    "your liked videos. Let's talk about what's pulling you in."
)

# ── Narrator voice candidates — conversational, not news-anchor ───────────────
# These are IDs of well-known conversational ElevenLabs voices.
# The script also discovers available voices from the library (probe 7) and
# can be re-run with VOICE_CANDIDATE_IDS pointing to any discovered IDs.
VOICE_CANDIDATE_IDS: list[str] = [
    "JBFqnCBsd6RMkjVDRZzb",  # George — Warm, Captivating Storyteller (premade)
    "SAz9YHcvj6GT2YYXdXww",  # River  — Relaxed, Neutral, Informative (premade)
    "cjVigY5qzO86Huf0OWal",  # Eric   — Smooth, Trustworthy (premade)
    "bIHbv24MWmeRgasZH58o",  # Will   — Relaxed Optimist (premade)
]

MODELS = ["eleven_turbo_v2_5", "eleven_flash_v2_5"]
FORMATS = ["mp3_44100_128", "mp3_22050_32"]
N_RUNS = 5
N_CONCURRENT = 4

# ── Output setup ──────────────────────────────────────────────────────────────

def make_output_dir() -> Path:
    ts = int(time.time())
    out = Path(f"tmp/audio_probe/probe_{ts}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "voices").mkdir(exist_ok=True)
    return out


def save_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2))
    print(f"  saved → {path}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def collect_bytes(gen) -> bytes:
    """Exhaust a bytes generator and return concatenated bytes."""
    chunks = []
    for chunk in gen:
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


# ── Probe 1 + 2 (+ Probe 3 model comparison) ─────────────────────────────────

def run_batch_latency(
    client: ElevenLabs,
    voice_id: str,
    model_id: str,
    output_format: str,
    n_runs: int,
    quiet: bool = False,
) -> list[float]:
    """Return list of wall-clock seconds for batch TTS (full MP3 returned)."""
    times: list[float] = []
    for i in range(n_runs):
        t0 = time.perf_counter()
        gen = client.text_to_speech.convert(
            voice_id=voice_id,
            text=SAMPLE_TEXT,
            model_id=model_id,
            output_format=output_format,
            voice_settings={
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        )
        _ = collect_bytes(gen)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        if not quiet:
            print(f"    run {i + 1}/{n_runs}: {elapsed:.2f}s")
    return times


def run_streaming_latency(
    client: ElevenLabs,
    voice_id: str,
    model_id: str,
    n_runs: int,
) -> list[dict]:
    """Return list of {first_chunk_s, full_file_s} for streaming TTS."""
    results: list[dict] = []
    for i in range(n_runs):
        t0 = time.perf_counter()
        first_chunk_time = None
        gen = client.text_to_speech.stream(
            voice_id=voice_id,
            text=SAMPLE_TEXT,
            model_id=model_id,
            output_format="mp3_44100_128",
            voice_settings={
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        )
        for chunk in gen:
            if chunk:
                if first_chunk_time is None:
                    first_chunk_time = time.perf_counter() - t0
        full_file_time = time.perf_counter() - t0
        results.append({"first_chunk_s": first_chunk_time, "full_file_s": full_file_time})
        print(f"    run {i + 1}/{n_runs}: first_chunk={first_chunk_time:.2f}s  full={full_file_time:.2f}s")
    return results


# ── Probe 4: voice audition ───────────────────────────────────────────────────

def run_voice_audition(
    client: ElevenLabs,
    voice_ids: list[str],
    model_id: str,
    out_dir: Path,
) -> list[dict]:
    """Generate sample text on each candidate voice, save MP3s."""
    results: list[dict] = []
    voices_dir = out_dir / "voices"
    for vid in voice_ids:
        print(f"  audition voice: {vid}")
        t0 = time.perf_counter()
        try:
            gen = client.text_to_speech.convert(
                voice_id=vid,
                text=SAMPLE_TEXT,
                model_id=model_id,
                output_format="mp3_44100_128",
                voice_settings={
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True,
                },
            )
            data = collect_bytes(gen)
            elapsed = time.perf_counter() - t0
            mp3_path = voices_dir / f"{vid}.mp3"
            mp3_path.write_bytes(data)
            results.append({
                "voice_id": vid,
                "generation_time_s": round(elapsed, 2),
                "file_size_bytes": len(data),
                "mp3_path": str(mp3_path),
            })
            print(f"    {vid}: {elapsed:.2f}s  {len(data):,} bytes  → {mp3_path}")
        except Exception as e:
            results.append({"voice_id": vid, "error": str(e)})
            print(f"    {vid}: ERROR {e}")
    return results


# ── Probe 5: output format comparison ────────────────────────────────────────

def run_format_comparison(
    client: ElevenLabs,
    voice_id: str,
    model_id: str,
    formats: list[str],
    out_dir: Path,
) -> list[dict]:
    results: list[dict] = []
    for fmt in formats:
        print(f"  format: {fmt}")
        t0 = time.perf_counter()
        gen = client.text_to_speech.convert(
            voice_id=voice_id,
            text=SAMPLE_TEXT,
            model_id=model_id,
            output_format=fmt,
            voice_settings={
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        )
        data = collect_bytes(gen)
        elapsed = time.perf_counter() - t0
        mp3_path = out_dir / f"format_{fmt}.mp3"
        mp3_path.write_bytes(data)
        results.append({
            "format": fmt,
            "generation_time_s": round(elapsed, 2),
            "file_size_bytes": len(data),
            "mp3_path": str(mp3_path),
        })
        print(f"    {fmt}: {elapsed:.2f}s  {len(data):,} bytes")
    return results


# ── Probe 6: concurrent batch ─────────────────────────────────────────────────

async def _single_batch_async(
    api_key: str,
    voice_id: str,
    model_id: str,
    run_index: int,
) -> dict:
    """One async batch request. Creates its own client (thread-safe)."""
    client = ElevenLabs(api_key=api_key)
    t0 = time.perf_counter()
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: collect_bytes(
                client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=SAMPLE_TEXT,
                    model_id=model_id,
                    output_format="mp3_44100_128",
                    voice_settings={
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                        "style": 0.0,
                        "use_speaker_boost": True,
                    },
                )
            ),
        )
        elapsed = time.perf_counter() - t0
        return {
            "run": run_index,
            "elapsed_s": round(elapsed, 2),
            "file_size_bytes": len(data),
            "status": "ok",
        }
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return {"run": run_index, "elapsed_s": round(elapsed, 2), "status": "error", "error": str(e)}


async def run_concurrent_batch(
    api_key: str,
    voice_id: str,
    model_id: str,
    n: int,
) -> dict:
    """Fire n batch requests in parallel, return timing and throttle verdict."""
    wall_t0 = time.perf_counter()
    tasks = [_single_batch_async(api_key, voice_id, model_id, i) for i in range(n)]
    individual = await asyncio.gather(*tasks)
    wall_elapsed = time.perf_counter() - wall_t0

    errors = [r for r in individual if r["status"] == "error"]
    throttled = any("429" in r.get("error", "") or "rate" in r.get("error", "").lower() for r in errors)
    times = [r["elapsed_s"] for r in individual if r["status"] == "ok"]

    return {
        "n_requests": n,
        "wall_clock_s": round(wall_elapsed, 2),
        "individual": individual,
        "errors": len(errors),
        "throttled": throttled,
        "p50_individual_s": round(percentile(times, 50), 2) if times else None,
        "p95_individual_s": round(percentile(times, 95), 2) if times else None,
        "gate_passed": not throttled and len(errors) == 0,
    }


# ── Probe 7: pay-as-you-go check ─────────────────────────────────────────────

def run_pagc_check(client: ElevenLabs) -> dict:
    """List available voices, surface any quota/tier info."""
    try:
        voices_resp = client.voices.get_all()
        voices = voices_resp.voices if hasattr(voices_resp, "voices") else []
        voice_list = []
        for v in voices:
            voice_list.append({
                "voice_id": v.voice_id,
                "name": v.name,
                "category": getattr(v, "category", None),
                "labels": dict(v.labels) if hasattr(v, "labels") and v.labels else {},
            })
        return {"total_voices": len(voice_list), "voices": voice_list}
    except Exception as e:
        print(f"  voices.get_all() skipped: {e}")
        return {
            "total_voices": None,
            "voices": [],
            "note": f"voices_read permission not available on this key: {e}",
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ElevenLabs API probe for audio component")
    parser.add_argument("--api-key", default=os.environ.get("ELEVEN_API_KEY", ""))
    parser.add_argument("--voice-id", default=VOICE_CANDIDATE_IDS[0],
                        help="Primary voice ID for latency/format probes")
    parser.add_argument("--skip-streaming", action="store_true",
                        help="Skip streaming latency probe (saves API quota)")
    args = parser.parse_args()

    api_key = args.api_key
    if not api_key:
        print("ERROR: set ELEVEN_API_KEY env var or pass --api-key", file=sys.stderr)
        sys.exit(1)

    client = ElevenLabs(api_key=api_key)
    primary_voice = args.voice_id
    primary_model = MODELS[0]  # eleven_turbo_v2_5 (used for single-model probes)

    out_dir = make_output_dir()
    print(f"\nOutput dir: {out_dir}\n")

    summary: dict = {}

    # ── Probe 7 first (cheap — no TTS quota used) ─────────────────────────────
    print("=" * 60)
    print("Probe 7: Pay-as-you-go check")
    print("=" * 60)
    p7 = run_pagc_check(client)
    save_json(out_dir / "07_pagc_check.json", p7)
    total_v = p7["total_voices"]
    print(f"  Total voices in library: {total_v if total_v is not None else 'N/A (voices_read permission missing)'}")
    summary["total_voices"] = total_v

    # ── Probe 3: model comparison (covers probes 1+2 on both models) ──────────
    print("\n" + "=" * 60)
    print("Probe 3: Model comparison (batch + streaming × 2 models)")
    print("=" * 60)
    model_results: list[dict] = []
    for model_id in MODELS:
        print(f"\n  Model: {model_id}")

        # Batch
        print(f"  Batch ({N_RUNS} runs):")
        batch_times = run_batch_latency(client, primary_voice, model_id, "mp3_44100_128", N_RUNS)
        batch_p50 = percentile(batch_times, 50)
        batch_p95 = percentile(batch_times, 95)
        print(f"  → batch p50={batch_p50:.2f}s  p95={batch_p95:.2f}s")

        # Streaming (informational)
        streaming_result = None
        if not args.skip_streaming:
            print(f"  Streaming ({N_RUNS} runs):")
            stream_runs = run_streaming_latency(client, primary_voice, model_id, N_RUNS)
            fc_times = [r["first_chunk_s"] for r in stream_runs if r.get("first_chunk_s")]
            ff_times = [r["full_file_s"] for r in stream_runs]
            streaming_result = {
                "runs": stream_runs,
                "first_chunk_p50_s": round(percentile(fc_times, 50), 2) if fc_times else None,
                "first_chunk_p95_s": round(percentile(fc_times, 95), 2) if fc_times else None,
                "full_file_p50_s": round(percentile(ff_times, 50), 2),
                "full_file_p95_s": round(percentile(ff_times, 95), 2),
            }
            print(f"  → streaming first_chunk p50={streaming_result['first_chunk_p50_s']:.2f}s  full_file p50={streaming_result['full_file_p50_s']:.2f}s")

        model_results.append({
            "model_id": model_id,
            "batch": {
                "runs_s": [round(t, 3) for t in batch_times],
                "p50_s": round(batch_p50, 2),
                "p95_s": round(batch_p95, 2),
                "gate_passed": batch_p50 < 10.0,
            },
            "streaming": streaming_result,
        })

    save_json(out_dir / "03_model_comparison.json", model_results)

    # Also save probes 1 & 2 as standalone files for parity with spec
    turbo = next(r for r in model_results if r["model_id"] == "eleven_turbo_v2_5")
    flash = next(r for r in model_results if r["model_id"] == "eleven_flash_v2_5")
    save_json(out_dir / "02_batch_latency.json", {
        "voice_id": primary_voice,
        "format": "mp3_44100_128",
        "n_runs": N_RUNS,
        "eleven_turbo_v2_5": turbo["batch"],
        "eleven_flash_v2_5": flash["batch"],
        "primary_gate": {
            "requirement": "p50 < 10s (eleven_turbo_v2_5)",
            "passed": turbo["batch"]["gate_passed"],
            "p50_s": turbo["batch"]["p50_s"],
        },
    })
    if not args.skip_streaming:
        save_json(out_dir / "01_streaming_latency.json", {
            "voice_id": primary_voice,
            "n_runs": N_RUNS,
            "eleven_turbo_v2_5": turbo["streaming"],
            "eleven_flash_v2_5": flash["streaming"],
            "note": "Informational only (eng review 2026-04-16). Streaming dropped for v0.",
        })

    summary["batch_p50_turbo_s"] = turbo["batch"]["p50_s"]
    summary["batch_p50_flash_s"] = flash["batch"]["p50_s"]
    summary["batch_gate_turbo"] = turbo["batch"]["gate_passed"]
    summary["batch_gate_flash"] = flash["batch"]["gate_passed"]

    # ── Probe 4: voice audition ───────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Probe 4: Voice audition")
    print("=" * 60)
    p4 = run_voice_audition(client, VOICE_CANDIDATE_IDS, primary_model, out_dir)
    save_json(out_dir / "04_voice_audition.json", p4)
    summary["voice_candidates"] = [
        {"voice_id": r["voice_id"], "mp3": r.get("mp3_path", ""), "gen_s": r.get("generation_time_s")}
        for r in p4
    ]

    # ── Probe 5: output format ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Probe 5: Output format comparison")
    print("=" * 60)
    p5 = run_format_comparison(client, primary_voice, primary_model, FORMATS, out_dir)
    save_json(out_dir / "05_output_format.json", p5)
    fmt_128 = next((r for r in p5 if "128" in r["format"]), {})
    fmt_32 = next((r for r in p5 if "32" in r.get("format", "")), {})
    if fmt_128 and fmt_32:
        size_128 = fmt_128.get("file_size_bytes", 0)
        size_32 = fmt_32.get("file_size_bytes", 0)
        reduction_pct = round((1 - size_32 / size_128) * 100, 1) if size_128 > 0 else 0
        print(f"  Size reduction 44100_128 → 22050_32: {reduction_pct}%")
        summary["format_size_reduction_pct"] = reduction_pct

    # ── Probe 6: concurrent batch ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Probe 6: Concurrent batch (4 parallel requests)")
    print("=" * 60)
    p6 = asyncio.run(run_concurrent_batch(api_key, primary_voice, primary_model, N_CONCURRENT))
    save_json(out_dir / "06_concurrent_batch.json", p6)
    print(f"  Wall clock: {p6['wall_clock_s']}s  errors: {p6['errors']}  throttled: {p6['throttled']}")
    print(f"  Gate passed: {p6['gate_passed']}")
    summary["concurrent_gate_passed"] = p6["gate_passed"]
    summary["concurrent_throttled"] = p6["throttled"]
    summary["concurrent_errors"] = p6["errors"]

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    batch_gate_ok = summary.get("batch_gate_turbo", False)
    concurrent_gate_ok = summary.get("concurrent_gate_passed", False)
    voice_candidates_ok = len([v for v in summary.get("voice_candidates", []) if not v.get("error")]) >= 1

    print(f"\n  [{'PASS' if batch_gate_ok else 'FAIL'}] Batch p50 < 10s (turbo): {summary.get('batch_p50_turbo_s')}s")
    print(f"  [{'PASS' if concurrent_gate_ok else 'FAIL'}] Concurrent batch (no throttling): {N_CONCURRENT} requests")
    print(f"  [{'PASS' if voice_candidates_ok else 'FAIL'}] Voice candidates available: {len(summary.get('voice_candidates', []))}")
    print(f"\n  Batch p50 flash:  {summary.get('batch_p50_flash_s')}s")
    print(f"  Total voices in library: {summary.get('total_voices')}")
    if "format_size_reduction_pct" in summary:
        print(f"  Format size reduction (128→32): {summary['format_size_reduction_pct']}%")

    all_gates = batch_gate_ok and concurrent_gate_ok and voice_candidates_ok
    print(f"\n  Overall gate: {'ALL PASSED ✓' if all_gates else 'SOME FAILED — see JSON results'}")

    print(f"\n  Voice candidates to audition (listen on headphones):")
    for v in summary.get("voice_candidates", []):
        if v.get("mp3"):
            print(f"    {v['voice_id']}: {v['mp3']}")

    print(f"\n  Streaming latency (informational — v1 data):")
    if turbo.get("streaming"):
        print(f"    turbo first_chunk p50: {turbo['streaming'].get('first_chunk_p50_s')}s  full_file p50: {turbo['streaming'].get('full_file_p50_s')}s")
    if flash.get("streaming"):
        print(f"    flash first_chunk p50: {flash['streaming'].get('first_chunk_p50_s')}s  full_file p50: {flash['streaming'].get('full_file_p50_s')}s")

    summary["gates"] = {
        "batch_p50_lt_10s": batch_gate_ok,
        "concurrent_no_throttle": concurrent_gate_ok,
        "voice_candidates_available": voice_candidates_ok,
        "all_passed": all_gates,
    }
    save_json(out_dir / "00_summary.json", summary)
    print(f"\n  Full results: {out_dir}/")


if __name__ == "__main__":
    main()
