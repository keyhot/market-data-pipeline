import pytest
from fastapi.testclient import TestClient

from api.main import app, metrics_registry

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_registry():
    metrics_registry.reset()
    yield
    metrics_registry.reset()


def test_metrics_records_requests_by_route_template():
    client.get("/health")
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total_requests"] == 2
    health = data["routes"]["GET /health"]
    assert health["count"] == 2
    assert health["statuses"] == {"200": 2}
    assert health["avg_ms"] >= 0


def test_metrics_endpoint_not_self_recorded():
    client.get("/metrics")

    response = client.get("/metrics")

    assert "GET /metrics" not in response.json()["data"]["routes"]


def test_metrics_records_error_statuses():
    client.get("/ticker/AAPL/invalid-range")

    response = client.get("/metrics")

    routes = response.json()["data"]["routes"]
    ticker_route = routes["GET /ticker/{ticker_symbol}/{time_range}"]
    assert ticker_route["statuses"] == {"422": 1}


def test_metrics_unmatched_path_bucketed():
    client.get("/definitely-not-a-route")

    response = client.get("/metrics")

    routes = response.json()["data"]["routes"]
    assert routes["GET (unmatched)"]["statuses"] == {"404": 1}
