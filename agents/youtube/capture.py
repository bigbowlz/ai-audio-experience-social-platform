"""YouTube Data API v3 capture — fetches the signals the agent needs.

Acquisition layer: runs after the user's OAuth flow for YouTube in the demo.
Fetches subscriptions, liked videos, channel topic details, and video topic
details, then writes the four JSON files the agent reads.

Public API:
    capture(session, out_dir) → Path   # call from demo after OAuth
    main()                             # standalone OAuth + capture

Standalone:
    python -m agents.youtube.capture [--out ydata/user]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession

_REPO_ROOT = Path(__file__).parents[2]
_DEFAULT_OUT = _REPO_ROOT / "ydata" / "user"
_CREDENTIALS = _REPO_ROOT / "app_credential" / "credentials.json"
_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
_API = "https://www.googleapis.com/youtube/v3"


# ── HTTP helpers ─────────────────────────────────────────────────────

def _get(session: AuthorizedSession, path: str, **params) -> dict:
    r = session.get(f"{_API}/{path}", params=params)
    r.raise_for_status()
    return r.json()


def _paginate(session: AuthorizedSession, path: str, **params) -> list[dict]:
    items: list[dict] = []
    page_token: str | None = None
    while True:
        p = dict(params)
        if page_token:
            p["pageToken"] = page_token
        data = _get(session, path, **p)
        items.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def _batched(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ── Capture ──────────────────────────────────────────────────────────

def _read_or_none(path: Path) -> dict | None:
    """Return parsed JSON if path exists and is non-empty, else None."""
    if path.exists() and path.stat().st_size > 0:
        with open(path) as f:
            return json.load(f)
    return None


def capture(
    session: AuthorizedSession,
    out_dir: Path,
    force: bool = False,
) -> Path:
    """Fetch YouTube signals and write the four JSON files to out_dir.

    Each numbered output file is a checkpoint: if it exists and is non-empty,
    that step is skipped. Pass force=True to re-fetch everything. Steps 3 and
    4 still need data from steps 1 and 2 (channel/video IDs), which is read
    back from disk on a skip.

    Returns out_dir (same path passed in, for callers that want to set
    YOUTUBE_PROBE_DIR from it).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_dir}")

    # 1) Subscriptions — all pages, snippet+contentDetails for subscribe timestamp
    subs_path = out_dir / "02_subscriptions.json"
    cached = None if force else _read_or_none(subs_path)
    if cached is not None:
        subs = cached["items"]
        print(f"  skip 02 (exists, {len(subs)} subscriptions)")
    else:
        print("→ subscriptions.list?mine=true (paginated)")
        subs = _paginate(
            session, "subscriptions",
            part="snippet,contentDetails",
            mine="true",
            maxResults=50,
        )
        subs_path.write_text(
            json.dumps({"count": len(subs), "items": subs}, indent=2, ensure_ascii=False)
        )
        print(f"  {len(subs)} subscriptions")

    # 2) Liked videos — all pages from LL playlist
    likes_path = out_dir / "03_likes.json"
    cached = None if force else _read_or_none(likes_path)
    if cached is not None:
        likes = cached["items"]
        print(f"  skip 03 (exists, {len(likes)} liked videos)")
    else:
        print("→ playlistItems.list?playlistId=LL (paginated)")
        likes = _paginate(
            session, "playlistItems",
            part="snippet,contentDetails",
            playlistId="LL",
            maxResults=50,
        )
        likes_path.write_text(
            json.dumps({"count": len(likes), "items": likes}, indent=2, ensure_ascii=False)
        )
        print(f"  {len(likes)} liked videos")

    # 3) Channel topic details — all subscribed channels, batched 50 at a time
    chan_path = out_dir / "07_topic_details.json"
    cached = None if force else _read_or_none(chan_path)
    if cached is not None:
        n = len(cached.get("items", []))
        print(f"  skip 07 ({n} channels with topic details cached)")
    else:
        print("→ channels.list?id=<all subs>&part=topicDetails (batched)")
        sub_channel_ids = [
            s["snippet"]["resourceId"]["channelId"]
            for s in subs
            if "snippet" in s and "resourceId" in s.get("snippet", {})
        ]
        channel_items: list[dict] = []
        for chunk in _batched(sub_channel_ids, 50):
            data = _get(
                session, "channels",
                part="snippet,topicDetails",
                id=",".join(chunk),
                maxResults=50,
            )
            channel_items.extend(data.get("items", []))
        chan_path.write_text(
            json.dumps({"items": channel_items}, indent=2, ensure_ascii=False)
        )
        covered = sum(
            1 for it in channel_items
            if it.get("topicDetails", {}).get("topicCategories")
        )
        print(f"  {covered}/{len(channel_items)} channels have topicDetails")

    # 4) Video topic details — all liked video IDs, batched 50 at a time
    vid_path = out_dir / "08_video_topic_details.json"
    cached = None if force else _read_or_none(vid_path)
    if cached is not None:
        n = len(cached.get("per_video", []))
        print(f"  skip 08 ({n} videos with topic details cached)")
    else:
        print("→ videos.list?id=<liked ids>&part=topicDetails,snippet (batched)")
        video_ids = [
            item["contentDetails"]["videoId"]
            for item in likes
            if item.get("contentDetails", {}).get("videoId")
        ]
        video_items: list[dict] = []
        for chunk in _batched(video_ids, 50):
            data = _get(
                session, "videos",
                part="snippet,topicDetails",
                id=",".join(chunk),
                maxResults=50,
            )
            video_items.extend(data.get("items", []))

        per_video = []
        for it in video_items:
            cats = it.get("topicDetails", {}).get("topicCategories", [])
            tags = [u.rsplit("/", 1)[-1] for u in cats]
            per_video.append({
                "id": it["id"],
                "title": it.get("snippet", {}).get("title", it["id"]),
                "channel": it.get("snippet", {}).get("channelTitle", "?"),
                "tags": tags,
                "category_id": it.get("snippet", {}).get("categoryId"),
            })

        returned_ids = {it["id"] for it in video_items}
        missing = [vid for vid in video_ids if vid not in returned_ids]
        with_topics = sum(1 for v in per_video if v["tags"])

        vid_path.write_text(
            json.dumps(
                {
                    "requested": len(video_ids),
                    "returned_by_api": len(video_items),
                    "missing_from_api": missing,
                    "with_nonempty_topicDetails": with_topics,
                    "per_video": per_video,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        print(f"  {with_topics}/{len(video_items)} videos have topicDetails")
    print(f"Done. Files written to {out_dir}")
    return out_dir


# ── OAuth + capture entry points ─────────────────────────────────────

def oauth_and_capture(
    out_dir: Path | None = None,
    credentials_path: Path | None = None,
    force: bool = False,
) -> Path:
    """Run the YouTube Data API OAuth flow and capture probe data.

    Opens the browser for consent (and prints the authorization URL to
    stdout as a fallback), fetches subscriptions/likes/topicDetails via
    youtube.readonly, and writes the probe JSON files to out_dir.

    Reused by preflight (auth/preflight.py::ensure_youtube_auth) and the
    standalone CLI (main()). Keeps the three-line OAuth boilerplate in
    one place.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

    out = Path(out_dir) if out_dir is not None else _DEFAULT_OUT
    creds_file = Path(credentials_path) if credentials_path is not None else _CREDENTIALS
    if not creds_file.exists():
        raise FileNotFoundError(
            f"YouTube OAuth client secrets not found: {creds_file}. "
            "Set YOUTUBE_OAUTH_CLIENT_SECRET or place credentials at "
            f"{creds_file} (default: {_CREDENTIALS})."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), _SCOPES)
    creds = flow.run_local_server(port=0)
    session = AuthorizedSession(creds)
    return capture(session, out, force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture YouTube signals via OAuth")
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_OUT,
        help=f"Output directory (default: {_DEFAULT_OUT})",
    )
    parser.add_argument(
        "--credentials", type=Path, default=_CREDENTIALS,
        help=f"OAuth desktop client secrets JSON (default: {_CREDENTIALS})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch all steps even if output files already exist.",
    )
    args = parser.parse_args()
    oauth_and_capture(out_dir=args.out, credentials_path=args.credentials, force=args.force)


if __name__ == "__main__":
    main()
