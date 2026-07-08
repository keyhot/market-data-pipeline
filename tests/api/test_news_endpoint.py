from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from config.exceptions import NoDataFoundError

client = TestClient(app)


def make_news_df():
    return pd.DataFrame(
        {
            "id": ["a"],
            "title": ["Some headline"],
            "publisher": ["Yahoo Finance"],
            "url": ["https://example.com/a"],
            "published_at": [pd.Timestamp("2026-07-01T10:00:00Z")],
            "summary": ["A summary"],
        }
    )


@patch("api.main.news_store")
@patch("api.main.fetch_news")
def test_news_success(mock_fetch, mock_store):
    mock_fetch.return_value = make_news_df()
    mock_store.save.return_value = "/tmp/news.csv"

    response = client.get("/news/AAPL")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ticker"] == "AAPL"
    assert data["count"] == 1
    assert data["items"][0]["title"] == "Some headline"
    assert data["items"][0]["published_at"] == "2026-07-01T10:00:00+00:00"
    assert data["file_path"] == "/tmp/news.csv"
    mock_fetch.assert_called_once_with("AAPL", limit=10, since=None)


@patch("api.main.news_store")
@patch("api.main.fetch_news")
def test_news_passes_limit_and_since(mock_fetch, mock_store):
    mock_fetch.return_value = make_news_df()
    mock_store.save.return_value = "/tmp/news.csv"

    response = client.get("/news/AAPL?limit=3&since=2026-06-01")

    assert response.status_code == 200
    mock_fetch.assert_called_once_with("AAPL", limit=3, since="2026-06-01")


@patch("api.main.fetch_news")
def test_news_not_found(mock_fetch):
    mock_fetch.side_effect = NoDataFoundError("No news found")

    response = client.get("/news/UNKNOWN")

    assert response.status_code == 404


def test_news_rejects_invalid_limit():
    assert client.get("/news/AAPL?limit=0").status_code == 422
    assert client.get("/news/AAPL?limit=101").status_code == 422
