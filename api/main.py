from fastapi import FastAPI
from fastapi import HTTPException
from config.logging import init_logging
from ingestion.fetcher import fetch_ticker
from ingestion.event_fetcher import fetch_events
from storage.filesystem import save_csv
from storage.naming import raw_data_path, raw_event_path
from schemas.responses import ApiResponse
from config.exceptions import NoDataFoundError, UnsupportedEventTypeError

app = FastAPI(title="Market Data Pipeline API")

logger = init_logging()
logger.info("API initialized")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ticker/{ticker_symbol}/{time_range}", response_model=ApiResponse)
def ticker(ticker_symbol: str, time_range: str):
    try:
        data = fetch_ticker(ticker_symbol, time_range)
        logger.info("Fetched ticker data", extra={"ticker_symbol": ticker_symbol, "time_range": time_range, "rows": len(data)})
        path = raw_data_path(ticker_symbol, time_range)
        save_csv(path, data)
        logger.info("Stored ticker data", extra={"file_path": path})
        return ApiResponse(
            status=200,
            data={
                "ticker": ticker_symbol,
                "rows": len(data),
                "file_path": str(path)
            }
        )
    except NoDataFoundError as e:
        raise HTTPException(status_code=404, detail="No data found" + str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail="Upstream service error" + str(e))

@app.get("/events/{ticker_symbol}/{event_type}", response_model=ApiResponse)
def event(ticker_symbol: str, event_type: str):
    try:
        events = fetch_events(ticker_symbol, event_type)
        logger.info("Fetched events", extra={"ticker_symbol": ticker_symbol, "event_type": event_type, "events": len(events)})
        path = raw_event_path(ticker_symbol, event_type)
        save_csv(path, events)
        logger.info("Stored events", extra={"file_path": path})
        return ApiResponse(
            status=200,
            data={
                "ticker": ticker_symbol,
                "event_type": event_type,
                "events": len(events),
                "file_path": str(path)
            }
        )
    except UnsupportedEventTypeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NoDataFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail="Upstream service error" + str(e))