from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from config.exceptions import NoDataFoundError

client = TestClient(app)


@patch("api.main.fetch_ticker_async")
def test_fetch_ticker_success(mock_fetch):
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    json_data = response.json()

    assert json_data["status"] == 200
    assert json_data["data"]["ticker"] == "AAPL"
    assert json_data["data"]["rows"] == 2


@patch("api.main.fetch_ticker_async")
def test_fetch_ticker_no_data(mock_fetch):
    mock_fetch.side_effect = NoDataFoundError("No data returned")

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 404


def test_fetch_ticker_invalid_time_range():
    response = client.get("/ticker/AAPL/invalid")

    assert response.status_code == 422


@patch("api.main.fetch_ticker_async")
def test_ticker_skips_csv_when_flag_disabled(mock_fetch, monkeypatch):
    monkeypatch.setenv("CSV_WRITE_ENABLED", "0")
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    with patch("api.main.save_csv") as save:
        response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    save.assert_not_called()
    assert "file_path" not in response.json()["data"]
