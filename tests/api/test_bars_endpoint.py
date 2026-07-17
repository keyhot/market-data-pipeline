from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)

BARS = [
    {
        "timestamp": "2026-01-05T00:00:00+00:00",
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "close": 100.0,
        "volume": 1000,
    }
]


def test_bars_returns_stored_bars():
    with patch("api.main.get_price_bars", return_value=BARS) as reader:
        response = client.get("/bars/aapl?limit=50")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ticker"] == "AAPL"
    assert data["interval"] == "1d"
    assert data["count"] == 1
    assert data["bars"] == BARS
    reader.assert_called_once_with("aapl", interval="1d", limit=50)


def test_bars_404_when_nothing_stored():
    with patch("api.main.get_price_bars", return_value=[]):
        response = client.get("/bars/ZZUNKNOWN")

    assert response.status_code == 404


def test_bars_503_when_postgres_unreachable():
    with patch("api.main.get_price_bars", side_effect=RuntimeError("db down")):
        response = client.get("/bars/AAPL")

    assert response.status_code == 503


def test_bars_rejects_bad_limit():
    response = client.get("/bars/AAPL?limit=0")

    assert response.status_code == 422
