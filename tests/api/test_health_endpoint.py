from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_includes_scheduler_status():
    response = client.get("/health")

    assert response.status_code == 200
    scheduler = response.json()["data"]["scheduler"]
    assert scheduler["running"] is False
    assert "enabled" in scheduler
    assert "jobs" in scheduler


def test_health_includes_postgres_status():
    response = client.get("/health")

    assert response.status_code == 200
    postgres = response.json()["data"]["postgres"]
    # Flag is off in tests, so no ping is attempted.
    assert postgres == {"enabled": False, "connected": None}


def test_health_includes_world_liveness():
    now = datetime.now(timezone.utc)
    with patch(
        "api.main.world_liveness_times",
        return_value=(now - timedelta(minutes=3), now - timedelta(minutes=1)),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    world = response.json()["data"]["world"]
    assert world["stale"] is False
    # Wall-clock read inside the handler, so bound it rather than pin it.
    assert 175 <= world["signal_age_s"] <= 185
    assert 55 <= world["event_age_s"] <= 65


def test_health_degrades_world_to_none_on_database_error():
    # KI-057's own endpoint must never 500 — a reader failure (DB down, or
    # any other exception, including the aware/naive TypeError a careless
    # caller could trigger) degrades this one field, not the response code.
    with patch(
        "api.main.world_liveness_times", side_effect=RuntimeError("connection lost")
    ):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["data"]["world"] is None


def test_metrics_includes_scheduler_status():
    response = client.get("/metrics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert "scheduler" in data
    assert "routes" in data


def test_metrics_includes_postgres_write_counters():
    response = client.get("/metrics")

    writes = response.json()["data"]["postgres_writes"]
    assert set(writes) == {
        "price_bars",
        "corporate_events",
        "news_items",
        "signals",
        "errors",
    }
