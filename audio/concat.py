"""Offline ffmpeg concat — single-file Episode MP3 for handoff.

NOT on the live-gen path. Runs post-rehearsal to produce a single MP3
for Slack/Drive handoff to judges.

Spec: audio/docs/DESIGN.md §Offline Concat
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


def build_concat_list(episode_dir: Path) -> list[str]:
    """Build ffmpeg concat-demuxer file lines, sorted numerically.

    Returns list of "file '<path>'" lines. macOS-safe (no ls -v).
    """
    files = sorted(
        episode_dir.glob("segment_*.mp3"),
        key=lambda f: int(re.search(r"segment_(\d+)", f.name).group(1)),
    )
    return [f"file '{f}'" for f in files]


def concat_episode(
    episode_dir: Path,
    episode_id: str,
    exports_dir: Path = Path("./exports"),
) -> Path:
    """Concatenate per-segment MP3s into a single episode file.

    Uses ffmpeg concat demuxer with -c copy (no re-encoding).

    Args:
        episode_dir: directory containing segment_N.mp3 files.
        episode_id: used for the output filename.
        exports_dir: output directory (created if absent).

    Returns:
        Path to the exported single-file MP3.

    Raises:
        ValueError: if no segment files found.
        subprocess.CalledProcessError: if ffmpeg fails.
    """
    lines = build_concat_list(episode_dir)
    if not lines:
        raise ValueError(f"No segment files found in {episode_dir}")

    exports_dir.mkdir(parents=True, exist_ok=True)
    output_path = exports_dir / f"episode-{episode_id}.mp3"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=True
    ) as concat_file:
        concat_file.write("\n".join(lines))
        concat_file.flush()

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file.name,
                "-c", "copy",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )

    return output_path
