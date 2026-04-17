"""One-time OAuth consent flow for Google Calendar API.

Usage:
    python auth/calendar_auth.py

Requires a Google Cloud OAuth client ID JSON file. Set the path via:
    GOOGLE_OAUTH_CLIENT_SECRET=path/to/client_secret.json

Stores the token at ~/.config/radio-podcast/calendar_token.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_DIR = Path.home() / ".config" / "radio-podcast"
TOKEN_PATH = TOKEN_DIR / "calendar_token.json"


_DEFAULT_CREDENTIALS = Path(__file__).parent / "credentials.json"


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


if __name__ == "__main__":
    main()
