import logging
import os

from ingestion.caching_provider import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_TTL_SECONDS,
    CachingProvider,
)
from ingestion.providers import MarketDataProvider
from ingestion.yfinance_provider import YFinanceProvider

CACHE_TTL_ENV = "CACHE_TTL_SECONDS"
CACHE_MAX_ENTRIES_ENV = "CACHE_MAX_ENTRIES"

_default_provider: MarketDataProvider | None = None
_logger = logging.getLogger(__name__)


def _read_positive_int(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "Ignoring non-integer %s=%r; using default %d", env_name, raw, default
        )
        return default
    if value <= 0:
        _logger.warning(
            "Ignoring non-positive %s=%d; using default %d", env_name, value, default
        )
        return default
    return value


def get_default_provider() -> MarketDataProvider:
    global _default_provider
    if _default_provider is None:
        ttl = _read_positive_int(CACHE_TTL_ENV, DEFAULT_TTL_SECONDS)
        max_entries = _read_positive_int(CACHE_MAX_ENTRIES_ENV, DEFAULT_MAX_ENTRIES)
        _default_provider = CachingProvider(
            YFinanceProvider(), ttl_seconds=ttl, max_entries=max_entries
        )
    return _default_provider


def reset_default_provider() -> None:
    global _default_provider
    _default_provider = None
