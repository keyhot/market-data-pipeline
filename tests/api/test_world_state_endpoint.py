"""Endpoint tests patch api.main.get_world_events — the projection itself is
covered by unit tests, so these assert wiring and the established error shape."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def _event(event_id, event_type="big_move", severity=6.0, payload=None):
    return {
        "id": event_id,
        "occurred_at": f"2026-07-20T12:{event_id:02d}:00+00:00",
        "event_type": event_type,
        "symbol": "BTCUSDT",
        "severity": severity,
        "payload": payload if payload is not None else {"return": 0.03},
    }


def test_world_state_returns_projection():
    with patch("api.main.get_world_events", return_value=[_event(1)]):
        response = client.get("/world/state")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["event_count"] == 1
    assert "BTCUSDT" in data["symbols"]
    assert data["recent"][0]["reaction"]["animation"] == "jolt"


def test_world_state_includes_model_record():
    events = [
        _event(1, "signal_resolved", 0.8, {"outcome": "win"}),
        _event(2, "signal_resolved", 1.4, {"outcome": "loss"}),
    ]
    with patch("api.main.get_world_events", return_value=events):
        data = client.get("/world/state").json()["data"]

    assert data["model"]["wins"] == 1
    assert data["model"]["losses"] == 1


def test_world_state_404_when_empty():
    with patch("api.main.get_world_events", return_value=[]):
        response = client.get("/world/state")

    assert response.status_code == 404


def test_world_state_503_when_postgres_down():
    with patch("api.main.get_world_events", side_effect=RuntimeError("no pool")):
        response = client.get("/world/state")

    assert response.status_code == 503


def test_world_state_rejects_out_of_range_limit():
    assert client.get("/world/state?limit=0").status_code == 422
    assert client.get("/world/state?limit=99999").status_code == 422


def test_world_state_is_stable_across_identical_calls():
    with patch("api.main.get_world_events", return_value=[_event(1), _event(2)]):
        first = client.get("/world/state").json()["data"]
        second = client.get("/world/state").json()["data"]

    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
