import pytest
import pandas as pd
from ingestion.event_fetcher import fetch_events
from config.exceptions import (
    UnsupportedEventTypeError,
    NoDataFoundError,
)


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

def test_fetch_events_invalid_type():
    provider = FakeProviderSuccess()

    with pytest.raises(UnsupportedEventTypeError):
        fetch_events("AAPL", "invalid", provider=provider)

def test_fetch_events_no_data():
    provider = FakeProviderEmpty()

    with pytest.raises(NoDataFoundError):
        fetch_events("AAPL", "dividends", provider=provider)