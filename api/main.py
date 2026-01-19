from fastapi import FastAPI
from ingestion.fetcher import fetch_ticker

app = FastAPI(title="Market Data Pipeline API")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ticker/{ticker_symbol}/{time_range}")
def ticker(ticker_symbol: str, time_range: str):
    data = fetch_ticker(ticker_symbol, time_range)
    return {"status": "ok", "message": "Ticker endpoint is under construction.", "data": data}