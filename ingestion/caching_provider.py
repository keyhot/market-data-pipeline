import threading
import time
from collections import OrderedDict
from typing import Any

from ingestion.providers import MarketDataProvider

DEFAULT_TTL_SECONDS = 60
DEFAULT_MAX_ENTRIES = 256


class CachingProvider(MarketDataProvider):
    def __init__(
        self,
        inner: MarketDataProvider,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Any = time.monotonic,
    ):
        self._inner = inner
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._store: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get_history(self, ticker_symbol: str, time_range: str):
        key = ("history", ticker_symbol, time_range)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        value = self._inner.get_history(ticker_symbol, time_range)
        self._store_value(key, value)
        return value

    def get_events(self, ticker_symbol: str, event_type: str):
        key = ("events", ticker_symbol, event_type)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        value = self._inner.get_events(ticker_symbol, event_type)
        self._store_value(key, value)
        return value

    def invalidate(self, key: tuple | None = None) -> None:
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)

    def _lookup(self, key: tuple):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def _store_value(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._store[key] = (self._clock() + self._ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)
