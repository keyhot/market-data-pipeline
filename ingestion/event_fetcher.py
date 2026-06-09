import pandas as pd

from config.exceptions import (
    BaseAppException,
    InvalidDateError,
    NoDataFoundError,
)
from ingestion.factory import get_default_provider
from ingestion.providers import MarketDataProvider


def fetch_events(
    ticker_symbol: str,
    event_type: str,
    start: str | None = None,
    end: str | None = None,
    provider: MarketDataProvider | None = None,
) -> pd.DataFrame:

    if provider is None:
        provider = get_default_provider()

    try:
        events = provider.get_events(ticker_symbol, event_type)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Failed to fetch events: {e}")

    if events is None or events.empty:
        raise NoDataFoundError("No events found for the given parameters")

    if isinstance(events, pd.Series):
        events = events.to_frame(name=event_type)

    try:
        start_ts = pd.to_datetime(start) if start is not None else None
        end_ts = pd.to_datetime(end) if end is not None else None
    except ValueError as e:
        raise InvalidDateError(f"Invalid date: {e}") from e

    if start_ts is not None:
        events = events[events.index >= start_ts]

    if end_ts is not None:
        events = events[events.index <= end_ts]

    if events.empty:
        raise NoDataFoundError("No events found for the given parameters")

    return events
