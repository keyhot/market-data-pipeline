"""Where the ticker and events endpoints put their CSV snapshots.

Sprint 19 draft KI "tests write into data/raw": test_fetch_ticker_success and the
two event tests mock only the fetcher, so the real handler writes a CSV (the flag
defaults on). tests/conftest.py now points storage.naming._DATA_ROOT at a tmp dir
for every test; these pin that the write lands there, by its real path, and not in
the checkout's data/.
"""

from pathlib import Path
from unittest.mock import patch

import pandas as pd
from data_guard import REPO_DATA_DIR
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


@patch("api.routers.market.fetch_ticker_async")
def test_the_ticker_snapshot_is_written_under_the_tmp_data_root(
    mock_fetch, tmp_path, monkeypatch
):
    # The compose containers set CSV_WRITE_ENABLED=0; pin the default (on).
    monkeypatch.delenv("CSV_WRITE_ENABLED", raising=False)
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    path = Path(response.json()["data"]["file_path"])
    assert path.is_relative_to(tmp_path / "data" / "raw" / "tickers")
    assert not path.resolve().is_relative_to(REPO_DATA_DIR.resolve())
    assert path.read_text() == ",Close\n0,100\n1,101\n"


@patch("api.routers.market.fetch_events")
def test_the_events_snapshot_is_written_under_the_tmp_data_root(
    mock_fetch, tmp_path, monkeypatch
):
    monkeypatch.delenv("CSV_WRITE_ENABLED", raising=False)
    mock_fetch.return_value = pd.DataFrame({"value": [1.0, 2.0]})

    response = client.get("/events/AAPL/dividends")

    assert response.status_code == 200
    path = Path(response.json()["data"]["file_path"])
    assert path.is_relative_to(tmp_path / "data" / "raw" / "events")
    assert not path.resolve().is_relative_to(REPO_DATA_DIR.resolve())
    assert path.read_text() == ",value\n0,1.0\n1,2.0\n"
