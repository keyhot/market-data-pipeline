from ingestion.caching_provider import CachingProvider
from ingestion.providers import MarketDataProvider
from ingestion.yfinance_provider import YFinanceProvider

_default_provider: MarketDataProvider | None = None


def get_default_provider() -> MarketDataProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = CachingProvider(YFinanceProvider())
    return _default_provider


def reset_default_provider() -> None:
    global _default_provider
    _default_provider = None
