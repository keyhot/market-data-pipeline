import pandas as pd
from config.exceptions import (
    UnsupportedEventTypeError,
    NoDataFoundError,
    BaseAppException,
)
from ingestion.yfinance_provider import YFinanceProvider


EVENT_TYPES = {"dividends", "splits", "actions"}


def fetch_events(
    ticker_symbol: str,
    event_type: str,
    start=None,
    end=None,
    provider=None,
) -> pd.DataFrame:

    if provider is None:
        provider = YFinanceProvider()

    event_type = event_type.lower()

    if event_type not in EVENT_TYPES:
        raise UnsupportedEventTypeError(
            f"Unsupported event_type: {event_type}"
        )

    try:
        events = provider.get_events(ticker_symbol, event_type)
    except Exception as e:
        raise BaseAppException(f"Failed to fetch events: {e}")

    if events is None or events.empty:
        raise NoDataFoundError("No events found for the given parameters")

    if isinstance(events, pd.Series):
        events = events.to_frame(name=event_type)

    if start is not None:
        start_ts = pd.to_datetime(start)
        events = events[events.index >= start_ts]

    if end is not None:
        end_ts = pd.to_datetime(end)
        events = events[events.index <= end_ts]

    if events.empty:
        raise NoDataFoundError("No events found for the given parameters")

    return events