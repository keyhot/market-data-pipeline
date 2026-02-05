from fastapi.testclient import TestClient
from api.main import app
from config.exceptions import NoDataFoundError
from unittest.mock import patch
import pandas as pd

client = TestClient(app)

@patch("api.main.fetch_ticker")
def test_ticker_endpoint_success(mock_fetch):
    mock_fetch.return_value = pd.DataFrame({"Close": [100]})

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    assert response.json()["status"] == 200

@patch("api.main.fetch_ticker")
def test_ticker_endpoint_no_data(mock_fetch):
    mock_fetch.side_effect = NoDataFoundError("No data")

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 404