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


class _Request:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeBroadcastsApi:
    """Mimics `youtube.liveBroadcasts()` — records the kwargs the client sends
    and serves pages keyed by pageToken."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        index = int(kwargs.get("pageToken") or 0)
        return _Request(self.pages[index])


def _client_with(api):
    """A YouTubeLiveClient wired to a fake API — bypasses __init__ so no OAuth,
    no googleapiclient, no network, while still exercising the real methods
    (the seam is exactly where this project's bugs have hidden)."""
    from broadcast.youtube_client import YouTubeLiveClient

    client = object.__new__(YouTubeLiveClient)
    client._yt = type("Yt", (), {"liveBroadcasts": lambda self: api})()
    return client


def test_known_broadcast_is_fetched_by_id_not_scanned():
    """Once we know our broadcast, ask for it by id. Scanning `mine=True` for
    it forever would put it at the mercy of the channel's completed-broadcast
    history: no ordering is guaranteed, so as history grows past one page our
    live broadcast falls off the list, select_broadcast returns None, tick
    reads unhealthy, and we orphan a new broadcast every backoff window."""
    api = _FakeBroadcastsApi(
        [{"items": [{"id": "b1", "status": {"lifeCycleStatus": "live"}}]}]
    )
    result = _client_with(api).list_broadcasts(broadcast_id="b1")
    assert [b["id"] for b in result] == ["b1"]
    assert api.calls[0]["id"] == "b1"
    assert "mine" not in api.calls[0]  # id and mine are mutually exclusive


def test_cold_start_scan_is_type_unfiltered_and_paginates():
    """`broadcastType="persistent"` cannot see the broadcasts we create — they
    carry a scheduledStartTime, making them *event* broadcasts, a distinct API
    category. And one page isn't the whole story once history accumulates."""
    api = _FakeBroadcastsApi(
        [
            {
                "items": [{"id": "old", "status": {"lifeCycleStatus": "complete"}}],
                "nextPageToken": "1",
            },
            {"items": [{"id": "b1", "status": {"lifeCycleStatus": "live"}}]},
        ]
    )
    result = _client_with(api).list_broadcasts()
    assert [b["id"] for b in result] == ["old", "b1"]
    assert api.calls[0]["mine"] is True
    assert api.calls[0]["broadcastType"] == "all"
    assert api.calls[1]["pageToken"] == "1"


def test_cold_start_scan_is_bounded():
    """A nextPageToken that never ends must not spin forever burning quota."""
    api = _FakeBroadcastsApi([{"items": [], "nextPageToken": "0"}])
    _client_with(api).list_broadcasts(max_pages=3)
    assert len(api.calls) == 3


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
