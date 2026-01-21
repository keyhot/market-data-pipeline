from fastapi import FastAPI
from ingestion.fetcher import fetch_ticker
from config.logging import init_logging
from storage.filesystem import save_csv
from storage.naming import raw_data_path

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