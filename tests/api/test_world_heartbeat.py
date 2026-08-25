from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_a_heartbeat_is_accepted_and_surfaces_in_health():
    client.post("/world/heartbeat", json={"page": "world", "frames": 42})
    data = client.get("/health").json()["data"]
    assert "renderer" in data
    assert data["renderer"]["healthy"] is True


def test_health_still_reports_postgres_alongside_the_renderer():
    """KI-024's probe reads data.postgres.connected. Adding a key must not move
    the one the watchdog already depends on."""
    data = client.get("/health").json()["data"]
    assert "postgres" in data and "scheduler" in data


def test_the_heartbeat_rejects_a_frame_count_that_is_not_a_number():
    response = client.post("/world/heartbeat", json={"page": "world", "frames": "lots"})
    assert response.status_code == 422


def test_the_beat_is_keyed_by_host_and_carries_its_frame_count():
    """The first test proves the `renderer` key exists — with an empty store
    `renderer_status` reads `healthy=True` from the startup grace period alone,
    so it would pass even if `record_beat` were never called. Host-keying is
    "the whole point" per the task brief; pin it directly. `frozen`/`healthy`
    timing semantics are the pure module's own contract, already covered by
    tests/unit/test_renderer_health.py — don't re-assert them here."""
    client.post("/world/heartbeat", json={"page": "world", "frames": 99})
    pages = client.get("/health").json()["data"]["renderer"]["pages"]
    assert "testserver" in pages  # TestClient's Host header
    assert pages["testserver"]["frames"] == 99
    assert pages["testserver"]["page"] == "world"
