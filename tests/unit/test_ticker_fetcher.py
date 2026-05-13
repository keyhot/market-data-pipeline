from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError

from config.exceptions import (
    DataProviderError,
    DataTooLargeError,
    NoDataFoundError,
)
from ingestion.fetcher import MAX_TICKER_ROWS, fetch_ticker


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


def test_fetch_ticker_too_large():
    class FakeProviderLarge:
        def get_history(self, ticker_symbol, time_range):
            return pd.DataFrame({"Close": range(MAX_TICKER_ROWS + 1)})

    with pytest.raises(DataTooLargeError):
        fetch_ticker("AAPL", "max", FakeProviderLarge())


@patch("ingestion.yfinance_provider.yf.Ticker")
def test_fetch_ticker_rate_limit(mock_ticker):
    mock_instance = MagicMock()
    mock_instance.history.side_effect = YFRateLimitError()

    mock_ticker.return_value = mock_instance

    with pytest.raises(DataProviderError):
        fetch_ticker("AAPL", "1d")
