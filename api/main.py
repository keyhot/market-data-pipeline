from fastapi import FastAPI
from config.logging import init_logging
from ingestion.fetcher import fetch_ticker
from ingestion.event_fetcher import fetch_events
from storage.filesystem import save_csv
from storage.naming import raw_data_path, raw_event_path

app = FastAPI(title="Market Data Pipeline API")

logger = init_logging()
logger.info("API initialized")
logger.info({"token": "abcd1234", "ticker": "AAPL"})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ticker/{ticker_symbol}/{time_range}")
def ticker(ticker_symbol: str, time_range: str):
    data = fetch_ticker(ticker_symbol, time_range)
    logger.info("Fetched ticker data", extra={"ticker_symbol": ticker_symbol, "time_range": time_range, "rows": len(data)})
    path = raw_data_path(ticker_symbol, time_range)
    save_csv(path, data)
    logger.info("Stored ticker data", extra={"file_path": path})

    return {
    "status": "ok",
    "ticker": ticker_symbol,
    "rows": len(data),
    "file_path": str(path),
}

@app.get("/events/{ticker}/{event_type}")
def event(ticker_symbol: str, event_type: str):
    events = fetch_events(ticker_symbol, event_type)
    logger.info("Fetched events", extra={"ticker_symbol": ticker_symbol, "event_type": event_type, "events": len(events)})
    path = raw_event_path(ticker_symbol, event_type)
    save_csv(path, events)
    logger.info("Stored events", extra={"file_path": path})
    return {
        "status": "ok",
        "ticker": ticker_symbol,
        "event_type": event_type,
        "events": len(events),
    }