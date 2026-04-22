"""Capture the external curator's YouTube data for the external agent.

The curator runs this once (Day 0). It authenticates via the same OAuth
desktop client as the dev probe, fetches subs + likes + channel/video topic
details, and writes the four JSON files that the shared extractor consumes.

Usage:
    python -m agents.external.capture

Requires:
    - agents/external/credentials.json (OAuth Desktop client, gitignored)
    - YouTube Data API v3 enabled on the GCP project
    - The curator added as test user in the OAuth consent screen

Output: agents/external/data/
    02_subscriptions.json  — subscription items
    03_likes.json          — liked-video playlist items
    07_topic_details.json  — channel topicCategories (all sub + like channels)
    08_video_topic_details.json — per-video topicCategories (all liked videos)
"""

import json
import time
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CREDENTIALS = HERE / "credentials.json"
OUT_DIR = HERE / "data"
API = "https://www.googleapis.com/youtube/v3"


def authenticate() -> AuthorizedSession:
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0)
    return AuthorizedSession(creds)


def get(session: AuthorizedSession, path: str, **params) -> dict:
    r = session.get(f"{API}/{path}", params=params)
    r.raise_for_status()
    return r.json()


def paginate(session: AuthorizedSession, path: str, **params) -> list[dict]:
    items: list[dict] = []
    page = None
    while True:
        p = {**params}
        if page:
            p["pageToken"] = page
        data = get(session, path, **p)
        items.extend(data.get("items", []))
        page = data.get("nextPageToken")
        if not page:
            break
    return items


def batched(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def dump(name: str, data: object) -> Path:
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output: {OUT_DIR}")
    print(f"Credentials: {CREDENTIALS}")
    if not CREDENTIALS.exists():
        raise FileNotFoundError(
            f"OAuth credentials not found at {CREDENTIALS}. "
            "Copy the Desktop OAuth client JSON there first."
        )

    session = authenticate()
    print("Authenticated.\n")

    # ── 1. Subscriptions ────────────────────────────────────────────
    print("→ subscriptions.list (paginated)")
    subs = paginate(
        session,
        "subscriptions",
        part="snippet,contentDetails",
        mine="true",
        maxResults=50,
    )
    dump("02_subscriptions", {"count": len(subs), "items": subs})
    print(f"  {len(subs)} subscriptions")

    # ── 2. Liked videos ─────────────────────────────────────────────
    print("→ playlistItems.list (LL playlist, paginated)")
    likes = paginate(
        session,
        "playlistItems",
        part="snippet,contentDetails",
        playlistId="LL",
        maxResults=50,
    )
    dump("03_likes", {"count": len(likes), "items": likes})
    print(f"  {len(likes)} liked videos")

    # ── 3. Channel topic details (ALL unique channels) ──────────────
    # Collect channel IDs from both subs and liked videos
    channel_ids: set[str] = set()
    for s in subs:
        cid = s.get("snippet", {}).get("resourceId", {}).get("channelId")
        if cid:
            channel_ids.add(cid)
    for l in likes:
        cid = l.get("snippet", {}).get("videoOwnerChannelId")
        if cid:
            channel_ids.add(cid)

    print(f"→ channels.list (topicDetails for {len(channel_ids)} unique channels)")
    all_channel_items: list[dict] = []
    for chunk in batched(sorted(channel_ids), 50):
        data = get(
            session,
            "channels",
            part="snippet,topicDetails",
            id=",".join(chunk),
            maxResults=50,
        )
        all_channel_items.extend(data.get("items", []))

    dump(
        "07_topic_details",
        {
            "kind": "youtube#channelListResponse",
            "pageInfo": {"totalResults": len(all_channel_items)},
            "items": all_channel_items,
        },
    )
    covered = sum(
        1
        for it in all_channel_items
        if it.get("topicDetails", {}).get("topicCategories")
    )
    print(f"  {len(all_channel_items)} channels returned, {covered} with topics")

    # ── 4. Video topic details (ALL liked videos) ───────────────────
    video_ids = [
        item["contentDetails"]["videoId"]
        for item in likes
        if item.get("contentDetails", {}).get("videoId")
    ]
    print(f"→ videos.list (topicDetails for {len(video_ids)} liked videos)")
    all_video_items: list[dict] = []
    for chunk in batched(video_ids, 50):
        data = get(
            session,
            "videos",
            part="snippet,topicDetails",
            id=",".join(chunk),
            maxResults=50,
        )
        all_video_items.extend(data.get("items", []))

    per_video = []
    with_topics = 0
    for it in all_video_items:
        cats = it.get("topicDetails", {}).get("topicCategories", [])
        tags = [c.rsplit("/", 1)[-1] for c in cats]
        per_video.append(
            {
                "id": it["id"],
                "title": it.get("snippet", {}).get("title", it["id"]),
                "channel": it.get("snippet", {}).get("channelTitle", "?"),
                "tags": tags,
            }
        )
        if tags:
            with_topics += 1

    missing_ids = [
        vid for vid in video_ids if vid not in {it["id"] for it in all_video_items}
    ]
    dump(
        "08_video_topic_details",
        {
            "requested": len(video_ids),
            "returned_by_api": len(all_video_items),
            "missing_from_api": missing_ids,
            "with_nonempty_topicDetails": with_topics,
            "per_video": per_video,
        },
    )
    print(
        f"  {len(all_video_items)} videos returned, {with_topics} with topics, {len(missing_ids)} missing"
    )

    # ── Summary ─────────────────────────────────────────────────────
    summary = [
        "# External Curator YouTube Data — Capture Summary",
        "",
        f"Captured: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- Subscriptions: {len(subs)}",
        f"- Liked videos: {len(likes)}",
        f"- Unique channels (subs + likes): {len(channel_ids)}",
        f"- Channels with topicCategories: {covered}/{len(all_channel_items)}",
        f"- Videos with topicCategories: {with_topics}/{len(all_video_items)}",
        f"- Videos missing from API (deleted/private): {len(missing_ids)}",
        "",
        "Files written:",
        "  02_subscriptions.json",
        "  03_likes.json",
        "  07_topic_details.json",
        "  08_video_topic_details.json",
    ]
    (OUT_DIR / "summary.md").write_text("\n".join(summary) + "\n")
    print(f"\nDone. Summary: {OUT_DIR / 'summary.md'}")


if __name__ == "__main__":
    run()
