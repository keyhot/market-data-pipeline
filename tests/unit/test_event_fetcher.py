from unittest.mock import MagicMock, PropertyMock, patch

import pandas as pd
import pytest

from config.exceptions import (
    BaseAppException,
    InvalidDateError,
    NoDataFoundError,
    UnsupportedEventTypeError,
)
from ingestion.event_fetcher import fetch_events
from ingestion.yfinance_provider import YFinanceProvider


class FakeProviderSuccess:
    def get_events(self, ticker_symbol, event_type):
        return pd.DataFrame({"value": [1, 2]})


class FakeProviderEmpty:
    def get_events(self, ticker_symbol, event_type):
        return pd.DataFrame()


class FakeProviderNone:
    def get_events(self, ticker_symbol, event_type):
        return None


class FakeProviderRaisesRaw:
    def get_events(self, ticker_symbol, event_type):
        raise RuntimeError("boom")


def test_fetch_events_success():
    provider = FakeProviderSuccess()

    result = fetch_events("AAPL", "dividends", provider=provider)

    assert not result.empty
    assert len(result) == 2


def test_fetch_events_no_data():
    provider = FakeProviderEmpty()

    with pytest.raises(NoDataFoundError):
        fetch_events("AAPL", "dividends", provider=provider)


def test_fetch_events_provider_returns_none_raises_no_data():
    # An empty frame also reaches the post-filter check; None only the first guard.
    provider = FakeProviderNone()

    with pytest.raises(NoDataFoundError) as exc_info:
        fetch_events("AAPL", "dividends", provider=provider)

    assert exc_info.value.status_code == 404
    assert exc_info.value.message == "No events found for the given parameters"


@pytest.mark.parametrize("start", ["notadate", "2023-01-01"])
def test_fetch_events_empty_frame_with_start_raises_no_data(start):
    # The first guard must fire before date handling: an empty frame plus an
    # invalid start would otherwise give InvalidDateError, and plus a valid
    # start a TypeError comparing its empty RangeIndex to a Timestamp.
    provider = FakeProviderEmpty()

    with pytest.raises(NoDataFoundError) as exc_info:
        fetch_events("AAPL", "dividends", start=start, provider=provider)

    assert exc_info.value.status_code == 404


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

    with pytest.raises(BaseAppException) as exc_info:
        fetch_events("AAPL", "dividends")

    # 503 comes from the provider's own wrap; fetch_events' wrap would give 500.
    assert exc_info.value.status_code == 503
    assert exc_info.value.message == "Failed to fetch events: boom"


def test_fetch_events_wraps_raw_provider_error():
    # A provider that does not wrap itself reaches fetch_events' own wrap (500).
    provider = FakeProviderRaisesRaw()

    with pytest.raises(BaseAppException) as exc_info:
        fetch_events("AAPL", "dividends", provider=provider)

    assert exc_info.value.status_code == 500
    assert exc_info.value.message == "Failed to fetch events: boom"


@patch("ingestion.yfinance_provider.yf.Ticker")
def test_unknown_event_type_raises_unsupported_error(mock_ticker):
    with pytest.raises(UnsupportedEventTypeError) as exc_info:
        YFinanceProvider().get_events("AAPL", "earnings")

    assert exc_info.value.status_code == 400


@patch("ingestion.yfinance_provider.yf.Ticker")
def test_fetch_events_propagates_unsupported_event_type(mock_ticker):
    with pytest.raises(UnsupportedEventTypeError):
        fetch_events("AAPL", "earnings")
