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
