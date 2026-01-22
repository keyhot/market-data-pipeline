import yfinance as yf
import pandas as pd
from config.exceptions import UnsupportedEventTypeError, NoDataFoundError

EVENT_MAP = {
    "dividends": lambda t: t.dividends,
    "splits": lambda t: t.splits,
    "actions": lambda t: t.actions,
}

def fetch_events(ticker_symbol: str, event_type: str, start=None, end=None):
    ticker = yf.Ticker(ticker_symbol)

    event_type = event_type.lower()

    if event_type not in EVENT_MAP:
        raise UnsupportedEventTypeError(f"Unsupported event_type: {event_type}")
    
    events = EVENT_MAP[event_type](ticker)

    if events is None:
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