"""One-shot OAuth consent for the YouTube Live broadcast manager (Sprint 14, A1).

Prereqs:
  1. Create a Google Cloud project (free).
  2. Enable the YouTube Data API v3.
  3. Create OAuth client credentials of type "Desktop app". Note the
     `client_id` and `client_secret`.
  4. Set `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET` in your
     `.env` (or export them in the shell that runs this script).

Run:
    poetry run python scripts/youtube_oauth_setup.py

The script opens a browser, you consent, and the refresh token is printed.
Copy it into `.env` as `YOUTUBE_OAUTH_REFRESH_TOKEN=...`. The token is **never**
committed and the script writes nothing to disk by default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main() -> int:
    _load_env_file(Path(__file__).resolve().parents[1] / ".env")

    cid = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID")
    secret = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    if not (cid and secret):
        print(
            "YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET must be set "
            "(in .env or exported). See scripts/youtube_oauth_setup.py docstring.",
            file=sys.stderr,
        )
        return 2

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }
    scopes = ["https://www.googleapis.com/auth/youtube"]
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    creds = flow.run_local_server(port=0)
    print()
    print("=" * 72)
    print("Paste this into your .env (NEVER commit it):")
    print(f"YOUTUBE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
