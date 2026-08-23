"""/music/now-playing — the credit the overlay renders."""

from datetime import datetime, timezone

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
