"""Tests for the YouTube Live API client seam (Sprint 14, Task A1).

The seam wraps YouTube Data API v3 (`liveStreams` / `liveBroadcasts`) so the
broadcast lifecycle can be tested with a `FakeYouTubeClient` and never touches
the network. OAuth is fail-closed: the manager refuses to boot without
`YOUTUBE_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN`.
"""

import pytest

from broadcast.youtube_client import MissingOAuth, credentials_from_env


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
