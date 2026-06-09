import pandas as pd
import pytest

from ingestion.caching_provider import CachingProvider


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class CountingProvider:
    def __init__(self):
        self.history_calls = 0
        self.events_calls = 0

    def get_history(self, ticker_symbol, time_range):
        self.history_calls += 1
        return pd.DataFrame({"Close": [1, 2], "_args": [ticker_symbol, time_range]})

    def get_events(self, ticker_symbol, event_type):
        self.events_calls += 1
        return pd.Series([1.5, 2.0], name=event_type)


def test_cache_miss_then_hit_returns_same_value_without_recalling_inner():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(0))

    first = cache.get_history("AAPL", "1d")
    second = cache.get_history("AAPL", "1d")

    assert inner.history_calls == 1
    pd.testing.assert_frame_equal(first, second)


def test_ttl_expiry_triggers_refetch():
    inner = CountingProvider()
    clock = FakeClock(0)
    cache = CachingProvider(inner, ttl_seconds=30, clock=clock)

    cache.get_history("AAPL", "1d")
    clock.advance(29)
    cache.get_history("AAPL", "1d")
    assert inner.history_calls == 1

    clock.advance(2)
    cache.get_history("AAPL", "1d")
    assert inner.history_calls == 2


def test_keys_are_separated_by_method_symbol_and_arg():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(0))

    cache.get_history("AAPL", "1d")
    cache.get_history("AAPL", "5d")
    cache.get_history("MSFT", "1d")
    cache.get_events("AAPL", "dividends")
    cache.get_events("AAPL", "splits")

    assert inner.history_calls == 3
    assert inner.events_calls == 2


def test_lru_eviction_at_max_entries():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, max_entries=2, clock=FakeClock(0))

    cache.get_history("AAPL", "1d")
    cache.get_history("MSFT", "1d")
    cache.get_history("AAPL", "1d")  # marks AAPL as recently used
    cache.get_history("TSLA", "1d")  # should evict MSFT

    assert inner.history_calls == 3

    cache.get_history("AAPL", "1d")  # still cached
    cache.get_history("TSLA", "1d")  # still cached
    cache.get_history("MSFT", "1d")  # was evicted → refetch
    assert inner.history_calls == 4


def test_inner_exceptions_are_not_cached():
    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        def get_history(self, ticker_symbol, time_range):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("upstream down")
            return pd.DataFrame({"Close": [1]})

        def get_events(self, ticker_symbol, event_type):
            return pd.Series()

    inner = FlakyProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(0))

    with pytest.raises(RuntimeError):
        cache.get_history("AAPL", "1d")

    df = cache.get_history("AAPL", "1d")
    assert not df.empty
    assert inner.calls == 2


def test_invalidate_specific_key():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(0))

    cache.get_history("AAPL", "1d")
    cache.get_history("MSFT", "1d")
    cache.invalidate(("history", "AAPL", "1d"))

    cache.get_history("AAPL", "1d")
    cache.get_history("MSFT", "1d")
    assert inner.history_calls == 3


def test_invalidate_all():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(0))

    cache.get_history("AAPL", "1d")
    cache.get_events("AAPL", "dividends")
    cache.invalidate()

    cache.get_history("AAPL", "1d")
    cache.get_events("AAPL", "dividends")
    assert inner.history_calls == 2
    assert inner.events_calls == 2
