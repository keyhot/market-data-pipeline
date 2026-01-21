import yfinance as yf
import pandas as pd

def fetch_events(ticker_symbol: str, event_type: str, start=None, end=None):
    ticker = yf.Ticker(ticker_symbol)
    if event_type.lower() == "dividends":
        events = ticker.dividends
    elif event_type.lower() == "splits":
        events = ticker.splits
    elif event_type.lower() == "actions":
        events = ticker.actions
    else:
        raise ValueError(f"Unsupported event_type: {event_type}")
    
    if start is not None:
        start_ts = pd.to_datetime(start)
        events = events[events.index >= start_ts]

    if end is not None:
        end_ts = pd.to_datetime(end)
        events = events[events.index <= end_ts]
    
    if isinstance(events, pd.Series):
        events = events.to_frame(name=event_type)
    return events