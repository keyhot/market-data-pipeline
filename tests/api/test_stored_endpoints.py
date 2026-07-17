from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)

EVENTS = [{"date": "2026-02-10", "event_type": "dividends", "value": 0.25}]
NEWS = [
    {
        "id": "story-1",
        "title": "AAPL soars",
        "publisher": "Wire",
        "url": "https://example.com/1",
        "published_at": "2026-03-01T00:00:00+00:00",
        "summary": "s1",
    }
]


def test_stored_events_returns_rows():
    with patch("api.main.get_corporate_events", return_value=EVENTS) as reader:
        response = client.get("/stored/events/aapl/dividends?limit=50")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ticker"] == "AAPL"
    assert data["count"] == 1
    assert data["events"] == EVENTS
    reader.assert_called_once_with("aapl", event_type="dividends", limit=50)


def test_stored_events_actions_reads_all_types():
    with patch("api.main.get_corporate_events", return_value=EVENTS) as reader:
        response = client.get("/stored/events/AAPL/actions")

    assert response.status_code == 200
    reader.assert_called_once_with("AAPL", event_type=None, limit=100)


def test_stored_events_404_when_empty():
    with patch("api.main.get_corporate_events", return_value=[]):
        assert client.get("/stored/events/ZZ/dividends").status_code == 404


def test_stored_events_503_when_postgres_down():
    with patch("api.main.get_corporate_events", side_effect=RuntimeError("down")):
        assert client.get("/stored/events/AAPL/dividends").status_code == 503


def test_stored_events_rejects_unknown_type():
    assert client.get("/stored/events/AAPL/earnings").status_code == 422


def test_stored_news_returns_rows():
    with patch("api.main.get_news_items", return_value=NEWS) as reader:
        response = client.get("/stored/news/aapl?limit=5")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ticker"] == "AAPL"
    assert data["count"] == 1
    assert data["items"] == NEWS
    reader.assert_called_once_with("aapl", limit=5)


def test_stored_news_404_when_empty():
    with patch("api.main.get_news_items", return_value=[]):
        assert client.get("/stored/news/ZZ").status_code == 404


def test_stored_news_503_when_postgres_down():
    with patch("api.main.get_news_items", side_effect=RuntimeError("down")):
        assert client.get("/stored/news/AAPL").status_code == 503
