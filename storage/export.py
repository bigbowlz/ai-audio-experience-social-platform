"""ffmpeg concat demuxer for judge-handoff MP3.

Input:  ./data/episodes/{episode_id}/segment_*.mp3 (sorted)
Output: ./exports/episode-{episode_id}.mp3

Uses ffmpeg's concat demuxer (needs a temporary filelist) so no re-encoding
happens — fast, lossless, preserves the ElevenLabs MP3s verbatim.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.4
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from storage.episode_dir import episode_dir

_EXPORTS_DIR = Path("exports")


def concat_episode_mp3(episode_id: str) -> Path:
    """Concat all segment_*.mp3 in the episode dir into exports/episode-{id}.mp3.

    Raises RuntimeError if no segments exist, ffmpeg is missing, or ffmpeg
    returns non-zero.
    """
    src_dir = episode_dir(episode_id)
    segments = sorted(src_dir.glob("segment_*.mp3"))
    if not segments:
        raise RuntimeError(
            f"no segment_*.mp3 files found in {src_dir}; cannot export episode."
        )
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install via `brew install ffmpeg` "
            "(macOS) and re-run; or skip export with `--no-export`."
        )

    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _EXPORTS_DIR / f"episode-{episode_id}.mp3"

    # Concat demuxer requires a newline-separated filelist with `file '…'` entries.
    # Use absolute paths to avoid cwd surprises.
    filelist = src_dir / "_concat_filelist.txt"
    filelist.write_text(
        "".join(f"file '{seg.resolve()}'\n" for seg in segments)
    )

    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0",
             "-i", str(filelist), "-c", "copy", str(out_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed ({result.returncode}): {result.stderr[:500]}"
            )
    finally:
        filelist.unlink(missing_ok=True)

    return out_path
