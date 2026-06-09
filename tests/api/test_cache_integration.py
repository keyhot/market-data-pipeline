from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from ingestion import factory

client = TestClient(app)


def setup_function(_):
    factory.reset_default_provider()


def teardown_function(_):
    factory.reset_default_provider()


@patch("api.main.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_ticker_second_request_is_cached_and_skips_save(mock_ticker, mock_save):
    instance = mock_ticker.return_value
    instance.history.return_value = pd.DataFrame({"Close": [100, 101]})

    r1 = client.get("/ticker/AAPL/1d")
    assert r1.status_code == 200
    body1 = r1.json()["data"]
    assert body1["cached"] is False
    assert "file_path" in body1
    assert mock_save.call_count == 1
    assert instance.history.call_count == 1

    r2 = client.get("/ticker/AAPL/1d")
    assert r2.status_code == 200
    body2 = r2.json()["data"]
    assert body2["cached"] is True
    assert "file_path" not in body2
    assert mock_save.call_count == 1
    assert instance.history.call_count == 1


@patch("api.main.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_events_second_request_is_cached_and_skips_save(mock_ticker, mock_save):
    instance = mock_ticker.return_value
    instance.dividends = pd.Series([1.0, 2.0], name="dividends")

    r1 = client.get("/events/AAPL/dividends")
    assert r1.status_code == 200
    body1 = r1.json()["data"]
    assert body1["cached"] is False
    assert "file_path" in body1
    assert mock_save.call_count == 1

    r2 = client.get("/events/AAPL/dividends")
    assert r2.status_code == 200
    body2 = r2.json()["data"]
    assert body2["cached"] is True
    assert "file_path" not in body2
    assert mock_save.call_count == 1


@patch("api.main.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_ticker_invalidate_forces_refetch_and_save(mock_ticker, mock_save):
    instance = mock_ticker.return_value
    instance.history.return_value = pd.DataFrame({"Close": [100, 101]})

    client.get("/ticker/AAPL/1d")
    assert mock_save.call_count == 1

    factory.get_default_provider().invalidate_history("AAPL", "1d")

    r2 = client.get("/ticker/AAPL/1d")
    assert r2.json()["data"]["cached"] is False
    assert "file_path" in r2.json()["data"]
    assert mock_save.call_count == 2
    assert instance.history.call_count == 2
