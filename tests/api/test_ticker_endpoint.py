from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from config.exceptions import NoDataFoundError

client = TestClient(app)


@patch("api.routers.market.fetch_ticker_async")
def test_fetch_ticker_success(mock_fetch):
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    json_data = response.json()

    assert json_data["status"] == 200
    assert json_data["data"]["ticker"] == "AAPL"
    assert json_data["data"]["rows"] == 2


@patch("api.routers.market.fetch_ticker_async")
def test_fetch_ticker_no_data(mock_fetch):
    mock_fetch.side_effect = NoDataFoundError("No data returned")

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 404


def test_fetch_ticker_invalid_time_range():
    response = client.get("/ticker/AAPL/invalid")

    assert response.status_code == 422


@patch("api.routers.market.fetch_ticker_async")
def test_ticker_skips_csv_when_flag_disabled(mock_fetch, monkeypatch):
    monkeypatch.setenv("CSV_WRITE_ENABLED", "0")
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    with patch("api.routers.market.save_csv") as save:
        response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    save.assert_not_called()
    assert "file_path" not in response.json()["data"]


@patch("api.routers.market.csv_write_enabled", return_value=False)
@patch("api.routers.market.fetch_ticker_async")
def test_the_postgres_write_runs_off_the_event_loop(mock_fetch, _csv):
    # KI-080: the handler is async, so a synchronous write on a stalled
    # database socket froze the whole event loop — every request, /health
    # included, whose own deadline thread could then never answer.
    import asyncio

    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})
    seen = {}

    def write(symbol, data):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False

    with (
        patch("api.routers.market.write_price_bars", side_effect=write),
        patch("api.routers.market.get_default_provider") as provider,
    ):
        provider.return_value.peek_history.return_value = None
        response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    assert seen == {"on_loop": False}
