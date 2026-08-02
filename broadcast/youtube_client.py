"""YouTube Data API v3 client seam for the broadcast lifecycle (Sprint 14, A1).

The only file in the package that imports googleapiclient — every other module
stays pure / network-free. `credentials_from_env` is fail-closed: the manager
refuses to boot without all three `YOUTUBE_OAUTH_*` env vars. Each method maps
the API response to a plain dict so `tick` stays pure and `FakeYouTubeClient`
is trivial.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = ["https://www.googleapis.com/auth/youtube"]


class MissingOAuth(RuntimeError):
    """Fail-closed: the broadcast manager never boots without its OAuth secret."""


def credentials_from_env() -> Credentials:
    """Build a refreshable Credentials object from `YOUTUBE_OAUTH_*` env vars.

    Raises:
        MissingOAuth: when any of `YOUTUBE_OAUTH_CLIENT_ID`,
            `YOUTUBE_OAUTH_CLIENT_SECRET`, or `YOUTUBE_OAUTH_REFRESH_TOKEN` is
            unset or empty.
    """
    cid = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID")
    secret = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    refresh = os.environ.get("YOUTUBE_OAUTH_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        raise MissingOAuth(
            "YOUTUBE_OAUTH_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN} must be set"
        )
    return Credentials(
        token=None,
        refresh_token=refresh,
        client_id=cid,
        client_secret=secret,
        token_uri=_TOKEN_URI,
        scopes=_SCOPES,
    )


def map_stream(item: dict) -> dict:
    """API `liveStreams` resource -> the plain dict `policy.tick` consumes.

    `status` is deliberately the *stream* status (`status.streamStatus`:
    active / created / error / inactive / ready) because that is the field
    `policy._stream_active` compares against `"active"` — the seam and the
    policy must agree on this shape or the manager silently never acts.
    """
    status = item.get("status", {})
    return {
        "id": item["id"],
        "title": item.get("snippet", {}).get("title"),
        "status": status.get("streamStatus"),
        # Health is reporting-only (good/ok/bad/noData) — never a gate, so a
        # "bad" ingest health can't stop us from taking the broadcast public.
        "health": (status.get("healthStatus") or {}).get("status"),
    }


def map_broadcast(item: dict) -> dict:
    """API `liveBroadcasts` resource -> the plain dict `policy.tick` consumes."""
    return {
        "id": item["id"],
        "lifecycle": item["status"]["lifeCycleStatus"],
        "privacy": item["status"].get("privacyStatus"),
        "bound_stream_id": item.get("contentDetails", {}).get("boundStreamId"),
    }


class YouTubeLiveClient:
    """Thin wrapper over YouTube Data API v3 `liveStreams` / `liveBroadcasts`.

    All methods return plain dicts (no google objects) so the lifecycle
    `tick()` stays pure and `FakeYouTubeClient` in tests is trivial. The
    constructor accepts an injected `credentials` for tests; default builds
    from env (fail-closed via `credentials_from_env`).
    """

    def __init__(self, credentials: Credentials | None = None) -> None:
        creds = credentials or credentials_from_env()
        # Imported lazily so the seam stays optional — only needed for the
        # real client. Tests use a FakeYouTubeClient and never import this.
        from googleapiclient.discovery import build

        self._yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    def list_broadcasts(self) -> list[dict]:
        resp = (
            self._yt.liveBroadcasts()
            .list(
                part="id,status,contentDetails",
                broadcastType="persistent",
                mine=True,
                maxResults=10,
            )
            .execute()
        )
        return [map_broadcast(item) for item in resp.get("items", [])]

    def find_stream(self, title: str) -> dict | None:
        resp = (
            self._yt.liveStreams()
            .list(part="id,cdn,status,snippet", mine=True, maxResults=50)
            .execute()
        )
        for item in resp.get("items", []):
            if item.get("snippet", {}).get("title") == title:
                return map_stream(item)
        return None

    def insert_broadcast(self, title: str, privacy: str = "public") -> dict:
        body = {
            "snippet": {
                "title": title,
                "scheduledStartTime": datetime.now(timezone.utc).isoformat(),
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": False,
                "enableDvr": True,
            },
        }
        item = (
            self._yt.liveBroadcasts()
            .insert(part="snippet,status,contentDetails", body=body)
            .execute()
        )
        return {
            "id": item["id"],
            "lifecycle": item["status"]["lifeCycleStatus"],
        }

    def bind_broadcast(self, broadcast_id: str, stream_id: str) -> dict:
        item = (
            self._yt.liveBroadcasts()
            .bind(
                id=broadcast_id,
                streamId=stream_id,
                part="id,contentDetails",
            )
            .execute()
        )
        return {
            "id": item["id"],
            "bound_stream_id": item.get("contentDetails", {}).get(
                "boundStreamId"
            ),
        }

    def transition(self, broadcast_id: str, status: str) -> dict:
        item = (
            self._yt.liveBroadcasts()
            .transition(broadcastStatus=status, id=broadcast_id, part="status")
            .execute()
        )
        return {
            "id": item["id"],
            "lifecycle": item["status"]["lifeCycleStatus"],
        }
