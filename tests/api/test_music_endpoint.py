"""/music/now-playing — the credit the overlay renders."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api.main as main

T0 = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)

ROW = {
    "file": "mixkit-sleepy-cat-135.mp3",
    "title": "Sleepy Cat",
    "artist": "Alejandro Magaña (A. M.)",
    "source": "Mixkit",
    "source_url": "https://mixkit.co/free-stock-music/lo-fi-beats/",
    "license": "Mixkit Stock Music Free License",
    "duration_seconds": 119.2,
    "started_at": T0,
}


@pytest.fixture
def client():
    return TestClient(main.app)


def test_it_returns_the_current_track(client, monkeypatch):
    monkeypatch.setattr(main, "get_now_playing", lambda: ROW)
    body = client.get("/music/now-playing").json()
    assert body["status"] == 200
    playing = body["data"]["playing"]
    assert playing["title"] == "Sleepy Cat"
    assert playing["artist"] == "Alejandro Magaña (A. M.)"
    assert playing["license"] == "Mixkit Stock Music Free License"


def test_no_bed_running_is_a_null_not_an_error(client, monkeypatch):
    """A stream with no music is a legitimate state; the strip shows nothing."""
    monkeypatch.setattr(main, "get_now_playing", lambda: None)
    response = client.get("/music/now-playing")
    assert response.status_code == 200
    assert response.json()["data"]["playing"] is None


def test_a_database_blip_does_not_500_the_overlay(client, monkeypatch):
    def boom():
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(main, "get_now_playing", boom)
    response = client.get("/music/now-playing")
    assert response.status_code == 200
    assert response.json()["data"]["playing"] is None


def test_the_strip_renders_the_credit_element(client):
    """The chip lives on the signals strip — the one band on air in all three
    live scenes — so crediting costs no extra browser source (KI-013)."""
    html = client.get("/overlay/signals").text
    assert 'id="np-title"' in html and 'id="np-artist"' in html
    assert "/music/now-playing" in html


def test_a_credit_outliving_its_track_is_dropped(client, monkeypatch):
    """The row is an upsert, so a dead runner leaves the last track in the
    table forever. Crediting a song over silence is the one failure this
    feature must not have — a voluntary credit naming the wrong track is
    worse than no credit."""
    stale = {**ROW, "started_at": datetime.now(timezone.utc) - timedelta(minutes=30)}
    monkeypatch.setattr(main, "get_now_playing", lambda: stale)
    assert client.get("/music/now-playing").json()["data"]["playing"] is None


def test_a_track_still_within_its_length_is_credited(client, monkeypatch):
    fresh = {**ROW, "started_at": datetime.now(timezone.utc) - timedelta(seconds=60)}
    monkeypatch.setattr(main, "get_now_playing", lambda: fresh)
    playing = client.get("/music/now-playing").json()["data"]["playing"]
    assert playing["title"] == "Sleepy Cat"


def test_the_credit_survives_the_gap_between_tracks(client, monkeypatch):
    """The runner polls every 5s, so a row is legitimately a few seconds past
    its length while the next track is being started. Blanking there would
    make the chip flicker on every change."""
    just_over = {
        **ROW,
        "started_at": datetime.now(timezone.utc)
        - timedelta(seconds=ROW["duration_seconds"] + 6),
    }
    monkeypatch.setattr(main, "get_now_playing", lambda: just_over)
    assert client.get("/music/now-playing").json()["data"]["playing"] is not None


def test_a_row_with_no_duration_still_expires(client, monkeypatch):
    unbounded = {
        **ROW,
        "duration_seconds": None,
        "started_at": datetime.now(timezone.utc) - timedelta(hours=4),
    }
    monkeypatch.setattr(main, "get_now_playing", lambda: unbounded)
    assert client.get("/music/now-playing").json()["data"]["playing"] is None
