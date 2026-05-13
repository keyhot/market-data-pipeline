from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from config.exceptions import NoDataFoundError

client = TestClient(app)


@patch("api.main.fetch_ticker")
def test_fetch_ticker_success(mock_fetch):
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    json_data = response.json()

    assert json_data["status"] == 200
    assert json_data["data"]["ticker"] == "AAPL"
    assert json_data["data"]["rows"] == 2


@patch("api.main.fetch_ticker")
def test_fetch_ticker_no_data(mock_fetch):
    mock_fetch.side_effect = NoDataFoundError("No data returned")

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 404


def test_fetch_ticker_invalid_time_range():
    response = client.get("/ticker/AAPL/invalid")

    assert response.status_code == 422
