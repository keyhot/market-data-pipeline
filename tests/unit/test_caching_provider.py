import pandas as pd
import pytest

from ingestion.caching_provider import CachingProvider

# F5 (audit 4): the production clock is time.monotonic(), which reads about a
# million seconds on a host that has been up a fortnight. A fake that starts at 0
# makes `clock() + ttl == ttl`, so a store that forgot the `clock() +` (and so
# expired every entry at once) stayed green. Start well away from zero. The .5 is
# exactly representable, so the boundary arithmetic below stays exact.
BOOT = 1_054_798.5


class FakeClock:
    def __init__(self, t: float = BOOT):
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
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    first = cache.get_history("AAPL", "1d")
    second = cache.get_history("AAPL", "1d")

    assert inner.history_calls == 1
    pd.testing.assert_frame_equal(first, second)


def test_ttl_expiry_triggers_refetch():
    inner = CountingProvider()
    clock = FakeClock(BOOT)
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
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    cache.get_history("AAPL", "1d")
    cache.get_history("AAPL", "5d")
    cache.get_history("MSFT", "1d")
    cache.get_events("AAPL", "dividends")
    cache.get_events("AAPL", "splits")

    assert inner.history_calls == 3
    assert inner.events_calls == 2


def test_lru_eviction_at_max_entries():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, max_entries=2, clock=FakeClock(BOOT))

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
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    with pytest.raises(RuntimeError):
        cache.get_history("AAPL", "1d")

    df = cache.get_history("AAPL", "1d")
    assert not df.empty
    assert inner.calls == 2


def test_invalidate_specific_key():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    cache.get_history("AAPL", "1d")
    cache.get_history("MSFT", "1d")
    cache.invalidate(("history", "AAPL", "1d"))

    cache.get_history("AAPL", "1d")
    cache.get_history("MSFT", "1d")
    assert inner.history_calls == 3


def test_invalidate_all():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    cache.get_history("AAPL", "1d")
    cache.get_events("AAPL", "dividends")
    cache.invalidate()

    cache.get_history("AAPL", "1d")
    cache.get_events("AAPL", "dividends")
    assert inner.history_calls == 2
    assert inner.events_calls == 2


def test_peek_history_returns_none_on_miss():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    assert cache.peek_history("AAPL", "1d") is None
    assert inner.history_calls == 0


def test_peek_history_returns_cached_on_hit_without_calling_inner():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    cache.get_history("AAPL", "1d")
    assert inner.history_calls == 1

    peeked = cache.peek_history("AAPL", "1d")
    assert peeked is not None
    assert inner.history_calls == 1


def test_peek_events_returns_none_on_miss_and_value_on_hit():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    assert cache.peek_events("AAPL", "dividends") is None
    cache.get_events("AAPL", "dividends")
    assert cache.peek_events("AAPL", "dividends") is not None
    assert inner.events_calls == 1


def test_invalidate_history_clears_specific_key():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    cache.get_history("AAPL", "1d")
    cache.get_history("MSFT", "1d")
    cache.invalidate_history("AAPL", "1d")

    assert cache.peek_history("AAPL", "1d") is None
    assert cache.peek_history("MSFT", "1d") is not None


def test_invalidate_events_clears_specific_key():
    inner = CountingProvider()
    cache = CachingProvider(inner, ttl_seconds=60, clock=FakeClock(BOOT))

    cache.get_events("AAPL", "dividends")
    cache.get_events("AAPL", "splits")
    cache.invalidate_events("AAPL", "dividends")

    assert cache.peek_events("AAPL", "dividends") is None
    assert cache.peek_events("AAPL", "splits") is not None


def test_base_provider_peek_and_invalidate_are_no_ops():
    from ingestion.providers import MarketDataProvider

    class TrivialProvider(MarketDataProvider):
        def get_history(self, t, r):
            return None

        def get_events(self, t, e):
            return None

    p = TrivialProvider()
    assert p.peek_history("AAPL", "1d") is None
    assert p.peek_events("AAPL", "dividends") is None
    p.invalidate_history("AAPL", "1d")
    p.invalidate_events("AAPL", "dividends")
