"""One-time OAuth consent flow for Google Calendar API + user profile.

Usage:
    python auth/calendar_auth.py

Requires a Google Cloud OAuth client ID JSON file. Set the path via:
    GOOGLE_OAUTH_CLIENT_SECRET=path/to/client_secret.json

Stores the token at ~/.config/radio-podcast/calendar_token.json and the
user profile (first_name, display_name, email) at
~/.config/radio-podcast/user_profile.json — consumed by the orchestrator
to thread Brief.user_profile through to the Producer's cold open.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
]
TOKEN_DIR = Path.home() / ".config" / "radio-podcast"
TOKEN_PATH = TOKEN_DIR / "calendar_token.json"
USER_PROFILE_PATH = TOKEN_DIR / "user_profile.json"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


_DEFAULT_CREDENTIALS = Path(__file__).parent / "credentials.json"


def _fetch_user_profile(access_token: str) -> dict:
    """Call Google's userinfo endpoint once and return the parsed response.

    Returns {} on failure — the caller writes whatever's there and the
    orchestrator treats an absent/empty profile as "address as 'you'".
    """
    req = urllib.request.Request(
        _USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[auth] userinfo fetch failed: {e}; proceeding with empty profile")
        return {}


def main() -> None:
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or (
        str(_DEFAULT_CREDENTIALS) if _DEFAULT_CREDENTIALS.exists() else None
    )
    if not client_secret:
        print(
            "Set GOOGLE_OAUTH_CLIENT_SECRET to the path of your OAuth client_secret JSON.\n"
            "  export GOOGLE_OAUTH_CLIENT_SECRET=/path/to/client_secret.json\n"
            "  python auth/calendar_auth.py"
        )
        sys.exit(1)

    if not Path(client_secret).exists():
        print(f"Client secret file not found: {client_secret}")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }))
    print(f"Token saved to {TOKEN_PATH}")

    userinfo = _fetch_user_profile(creds.token)
    profile = {
        "first_name": userinfo.get("given_name"),
        "display_name": userinfo.get("name"),
        "email": userinfo.get("email"),
    }
    USER_PROFILE_PATH.write_text(json.dumps(profile, indent=2))
    print(f"Profile saved to {USER_PROFILE_PATH} (first_name={profile['first_name']!r})")


if __name__ == "__main__":
    main()
