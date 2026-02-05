import pytest
import pandas as pd
from yfinance.exceptions import YFRateLimitError
from ingestion.fetcher import fetch_ticker
from unittest.mock import patch, MagicMock
from config.exceptions import NoDataFoundError, DataProviderError, BaseAppException


class FakeProviderSuccess:
    def get_history(self, ticker_symbol, time_range):
        return pd.DataFrame({"Close": [100, 101]})


class FakeProviderEmpty:
    def get_history(self, ticker_symbol, time_range):
        return pd.DataFrame()


def test_fetch_ticker_success():
    provider = FakeProviderSuccess()

    result = fetch_ticker("AAPL", "1d", provider)

    assert not result.empty
    assert len(result) == 2


def test_fetch_ticker_no_data():
    provider = FakeProviderEmpty()

    with pytest.raises(NoDataFoundError):
        fetch_ticker("AAPL", "1d", provider)


@patch("ingestion.fetcher.yf.Ticker")
def test_fetch_ticker_rate_limit(mock_ticker):
    mock_instance = MagicMock()
    mock_instance.history.side_effect = YFRateLimitError()

    mock_ticker.return_value = mock_instance

    with pytest.raises(DataProviderError):
        fetch_ticker("AAPL", "1d")