from unittest.mock import MagicMock, PropertyMock, patch

import pandas as pd
import pytest

from config.exceptions import (
    BaseAppException,
    InvalidDateError,
    NoDataFoundError,
)
from ingestion.event_fetcher import fetch_events


class FakeProviderSuccess:
    def get_events(self, ticker_symbol, event_type):
        return pd.DataFrame({"value": [1, 2]})


class FakeProviderEmpty:
    def get_events(self, ticker_symbol, event_type):
        return pd.DataFrame()


def test_fetch_events_success():
    provider = FakeProviderSuccess()

    result = fetch_events("AAPL", "dividends", provider=provider)

    assert not result.empty
    assert len(result) == 2


def test_fetch_events_no_data():
    provider = FakeProviderEmpty()

    with pytest.raises(NoDataFoundError):
        fetch_events("AAPL", "dividends", provider=provider)


def test_fetch_events_invalid_start_date():
    provider = FakeProviderSuccess()

    with pytest.raises(InvalidDateError):
        fetch_events("AAPL", "dividends", start="notadate", provider=provider)


def test_fetch_events_invalid_end_date():
    provider = FakeProviderSuccess()

    with pytest.raises(InvalidDateError):
        fetch_events("AAPL", "dividends", end="notadate", provider=provider)


@patch("ingestion.yfinance_provider.yf.Ticker")
def test_fetch_events_provider_generic_error(mock_ticker):
    mock_instance = MagicMock()
    type(mock_instance).dividends = PropertyMock(side_effect=RuntimeError("boom"))
    mock_ticker.return_value = mock_instance

    with pytest.raises(BaseAppException):
        fetch_events("AAPL", "dividends")
