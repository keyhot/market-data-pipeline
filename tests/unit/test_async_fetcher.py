import asyncio
import threading

import pandas as pd
import pytest

from config.exceptions import BaseAppException, NoDataFoundError
from ingestion.fetcher import fetch_ticker_async
from ingestion.providers import MarketDataProvider


class FakeProvider(MarketDataProvider):
    def get_history(self, ticker_symbol, time_range):
        return pd.DataFrame({"Close": [100, 101]})

    def get_events(self, ticker_symbol, event_type):
        return pd.DataFrame()


def test_fetch_ticker_async_success():
    result = asyncio.run(fetch_ticker_async("AAPL", "1d", FakeProvider()))

    assert len(result) == 2


def test_fetch_ticker_async_no_data():
    class EmptyProvider(FakeProvider):
        def get_history(self, ticker_symbol, time_range):
            return pd.DataFrame()

    with pytest.raises(NoDataFoundError):
        asyncio.run(fetch_ticker_async("AAPL", "1d", EmptyProvider()))


def test_fetch_ticker_async_wraps_unexpected_errors():
    class BrokenProvider(FakeProvider):
        def get_history(self, ticker_symbol, time_range):
            raise ConnectionError("network down")

    with pytest.raises(BaseAppException) as exc_info:
        asyncio.run(fetch_ticker_async("AAPL", "1d", BrokenProvider()))

    assert exc_info.value.status_code == 503


def test_gather_runs_provider_calls_concurrently():
    barrier = threading.Barrier(3, timeout=5)

    class BlockingProvider(FakeProvider):
        def get_history(self, ticker_symbol, time_range):
            # Only passes if all three fetches run at the same time.
            barrier.wait()
            return pd.DataFrame({"Close": [1]})

    async def fetch_all():
        provider = BlockingProvider()
        return await asyncio.gather(
            fetch_ticker_async("AAPL", "1d", provider),
            fetch_ticker_async("MSFT", "1d", provider),
            fetch_ticker_async("GOOG", "1d", provider),
        )

    results = asyncio.run(fetch_all())

    assert len(results) == 3
    assert all(len(df) == 1 for df in results)


def test_default_async_method_delegates_to_sync():
    calls = []

    class RecordingProvider(FakeProvider):
        def get_history(self, ticker_symbol, time_range):
            calls.append((ticker_symbol, time_range))
            return pd.DataFrame({"Close": [1]})

    result = asyncio.run(RecordingProvider().get_history_async("AAPL", "5d"))

    assert calls == [("AAPL", "5d")]
    assert len(result) == 1
