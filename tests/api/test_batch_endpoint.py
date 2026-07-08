from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import MAX_BATCH_SYMBOLS, app
from config.exceptions import NoDataFoundError

client = TestClient(app)


@patch("api.main.save_csv")
@patch("api.main.fetch_ticker_async")
def test_batch_fetch_success(mock_fetch, mock_save):
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    response = client.get("/tickers/1d?symbols=AAPL,MSFT")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["requested"] == 2
    assert data["succeeded"] == 2
    assert data["failed"] == 0
    assert data["results"]["AAPL"]["rows"] == 2
    assert data["results"]["MSFT"]["rows"] == 2


@patch("api.main.save_csv")
@patch("api.main.fetch_ticker_async")
def test_batch_fetch_partial_failure(mock_fetch, mock_save):
    async def fetch(symbol, time_range):
        if symbol == "MSFT":
            raise NoDataFoundError("No data returned")
        return pd.DataFrame({"Close": [100]})

    mock_fetch.side_effect = fetch

    response = client.get("/tickers/1d?symbols=AAPL,MSFT")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    assert data["results"]["AAPL"]["rows"] == 1
    assert data["results"]["MSFT"] == {"error": "No data returned", "status": 404}


@patch("api.main.save_csv")
@patch("api.main.fetch_ticker_async")
def test_batch_fetch_deduplicates_and_normalizes_symbols(mock_fetch, mock_save):
    mock_fetch.return_value = pd.DataFrame({"Close": [100]})

    response = client.get("/tickers/1d?symbols=aapl, AAPL ,msft")

    data = response.json()["data"]
    assert data["requested"] == 2
    assert set(data["results"]) == {"AAPL", "MSFT"}


def test_batch_fetch_rejects_too_many_symbols():
    symbols = ",".join(f"SYM{i}" for i in range(MAX_BATCH_SYMBOLS + 1))

    response = client.get(f"/tickers/1d?symbols={symbols}")

    assert response.status_code == 400


def test_batch_fetch_rejects_empty_symbols():
    response = client.get("/tickers/1d?symbols=,, ,")

    assert response.status_code == 400


def test_batch_fetch_requires_symbols_param():
    response = client.get("/tickers/1d")

    assert response.status_code == 422


def test_batch_fetch_invalid_time_range():
    response = client.get("/tickers/notarange?symbols=AAPL")

    assert response.status_code == 422
