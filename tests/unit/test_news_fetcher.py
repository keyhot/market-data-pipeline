import pandas as pd
import pytest

from config.exceptions import (
    BaseAppException,
    InvalidDateError,
    NoDataFoundError,
)
from ingestion.caching_provider import CachingProvider
from ingestion.news_fetcher import fetch_news, normalize_news


def make_raw_item(item_id, title, pub_date):
    return {
        "id": item_id,
        "content": {
            "id": item_id,
            "title": title,
            "pubDate": pub_date,
            "summary": f"summary of {title}",
            "provider": {"displayName": "Yahoo Finance"},
            "canonicalUrl": {"url": f"https://example.com/{item_id}"},
        },
    }


SAMPLE_RAW = [
    make_raw_item("a", "Old story", "2026-01-01T10:00:00Z"),
    make_raw_item("b", "New story", "2026-07-01T10:00:00Z"),
    make_raw_item("c", "Mid story", "2026-03-01T10:00:00Z"),
]


class FakeNewsProvider:
    def __init__(self, raw=None):
        self.raw = SAMPLE_RAW if raw is None else raw

    def get_news(self, ticker_symbol):
        return self.raw


def test_fetch_news_normalizes_and_sorts_newest_first():
    result = fetch_news("AAPL", provider=FakeNewsProvider())

    assert list(result["id"]) == ["b", "c", "a"]
    assert result.iloc[0]["title"] == "New story"
    assert result.iloc[0]["publisher"] == "Yahoo Finance"
    assert result.iloc[0]["url"] == "https://example.com/b"


def test_fetch_news_limit():
    result = fetch_news("AAPL", limit=1, provider=FakeNewsProvider())

    assert len(result) == 1
    assert result.iloc[0]["id"] == "b"


def test_fetch_news_since_filter():
    result = fetch_news("AAPL", since="2026-02-01", provider=FakeNewsProvider())

    assert list(result["id"]) == ["b", "c"]


def test_fetch_news_invalid_since_rejected():
    with pytest.raises(InvalidDateError):
        fetch_news("AAPL", since="notadate", provider=FakeNewsProvider())


def test_fetch_news_empty_raises_not_found():
    with pytest.raises(NoDataFoundError):
        fetch_news("AAPL", provider=FakeNewsProvider(raw=[]))


def test_fetch_news_filter_to_empty_raises_not_found():
    with pytest.raises(NoDataFoundError):
        fetch_news("AAPL", since="2027-01-01", provider=FakeNewsProvider())


def test_fetch_news_wraps_unexpected_errors():
    class BrokenProvider:
        def get_news(self, ticker_symbol):
            raise ConnectionError("network down")

    with pytest.raises(BaseAppException) as exc_info:
        fetch_news("AAPL", provider=BrokenProvider())

    assert exc_info.value.status_code == 503


def test_normalize_news_handles_missing_fields():
    result = normalize_news([{"id": "x", "content": {}}])

    assert result.iloc[0]["id"] == "x"
    assert pd.isna(result.iloc[0]["publisher"]) or result.iloc[0]["publisher"] is None


def test_caching_provider_caches_news():
    class CountingProvider(FakeNewsProvider):
        calls = 0

        def get_news(self, ticker_symbol):
            CountingProvider.calls += 1
            return self.raw

    cached = CachingProvider(CountingProvider())

    assert cached.peek_news("AAPL") is None
    cached.get_news("AAPL")
    cached.get_news("AAPL")

    assert CountingProvider.calls == 1
    assert cached.peek_news("AAPL") is not None
