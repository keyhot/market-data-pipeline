"""Tests for the YouTube Live API client seam (Sprint 14, Task A1).

The seam wraps YouTube Data API v3 (`liveStreams` / `liveBroadcasts`) so the
broadcast lifecycle can be tested with a `FakeYouTubeClient` and never touches
the network. OAuth is fail-closed: the manager refuses to boot without
`YOUTUBE_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN`.
"""

import pytest

from broadcast.policy import BroadcastConfig, BroadcastState, tick
from broadcast.youtube_client import (
    MissingOAuth,
    credentials_from_env,
    map_broadcast,
    map_stream,
)

# Shaped exactly like the YouTube Data API v3 `liveStreams` resource — the
# stream's state lives at `status.streamStatus` (active/created/error/
# inactive/ready), NOT at a bare `status`.
_API_STREAM = {
    "id": "s-1",
    "snippet": {"title": "Market Data Pipeline"},
    "status": {
        "streamStatus": "active",
        "healthStatus": {"status": "good"},
    },
}


def test_missing_oauth_fails_closed(monkeypatch):
    for k in (
        "YOUTUBE_OAUTH_CLIENT_ID",
        "YOUTUBE_OAUTH_CLIENT_SECRET",
        "YOUTUBE_OAUTH_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(MissingOAuth):
        credentials_from_env()


def test_credentials_built_when_env_present(monkeypatch):
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("YOUTUBE_OAUTH_REFRESH_TOKEN", "rtok")
    creds = credentials_from_env()
    assert creds.client_id == "cid"
    assert creds.client_secret == "csecret"
    assert creds.refresh_token == "rtok"


def test_mapped_stream_carries_the_status_the_policy_reads():
    """The seam and the policy have to agree on the dict shape. `find_stream`
    originally mapped only {id, title}, so `policy._stream_active` — which
    reads `stream["status"] == "active"` — was False on every tick and the
    manager would never have created or transitioned anything, with the whole
    suite green (A2's tests hand-build yt_state)."""
    mapped = map_stream(_API_STREAM)
    assert mapped["id"] == "s-1"
    assert mapped["title"] == "Market Data Pipeline"
    assert mapped["status"] == "active"
    assert mapped["health"] == "good"


def test_api_shaped_stream_drives_the_policy_to_act():
    """The end-to-end shape check: a real-shaped active stream + no broadcast
    must produce a create_and_bind. This is the test that fails if either side
    of the seam changes its dict shape."""
    _, actions = tick(
        {"broadcast": None, "stream": map_stream(_API_STREAM)},
        obs_streaming=True,
        state=BroadcastState(None, None, None, None),
        config=BroadcastConfig(),
        now=__import__("datetime").datetime(2026, 8, 2, tzinfo=None),
    )
    assert ("create_and_bind",) in actions


def test_inactive_stream_maps_to_a_non_active_status():
    inactive = {**_API_STREAM, "status": {"streamStatus": "inactive"}}
    mapped = map_stream(inactive)
    assert mapped["status"] == "inactive"
    assert mapped["health"] is None  # absent healthStatus must not raise


def test_mapped_broadcast_carries_lifecycle_and_binding():
    mapped = map_broadcast(
        {
            "id": "b-1",
            "status": {"lifeCycleStatus": "live", "privacyStatus": "public"},
            "contentDetails": {"boundStreamId": "s-1"},
        }
    )
    assert mapped == {
        "id": "b-1",
        "lifecycle": "live",
        "privacy": "public",
        "bound_stream_id": "s-1",
    }
