from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from config.exceptions import NoDataFoundError

client = TestClient(app)


@patch("api.main.fetch_events")
def test_fetch_events_success(mock_fetch):
    mock_fetch.return_value = pd.DataFrame({"value": [1.0, 2.0]})

    response = client.get("/events/AAPL/dividends")

    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == 200
    assert json_data["data"]["ticker"] == "AAPL"
    assert json_data["data"]["event_type"] == "dividends"
    assert json_data["data"]["events"] == 2
    assert json_data["data"]["start"] is None
    assert json_data["data"]["end"] is None
    mock_fetch.assert_called_once_with("AAPL", "dividends", start=None, end=None)


@patch("api.main.fetch_events")
def test_fetch_events_with_date_filters(mock_fetch):
    mock_fetch.return_value = pd.DataFrame({"value": [1.0]})

    response = client.get("/events/AAPL/dividends?start=2024-01-01&end=2024-12-31")

    assert response.status_code == 200
    json_data = response.json()
    assert json_data["data"]["start"] == "2024-01-01"
    assert json_data["data"]["end"] == "2024-12-31"
    mock_fetch.assert_called_once_with(
        "AAPL", "dividends", start="2024-01-01", end="2024-12-31"
    )


@patch("api.main.fetch_events")
def test_fetch_events_no_data(mock_fetch):
    mock_fetch.side_effect = NoDataFoundError("No events found")

    response = client.get("/events/AAPL/dividends")

    assert response.status_code == 404


def test_fetch_events_invalid_type():
    response = client.get("/events/AAPL/invalid")

    assert response.status_code == 422
