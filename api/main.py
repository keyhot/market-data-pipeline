import time
from enum import Enum

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.metrics import UNMATCHED_ROUTE, MetricsRegistry
from config.exceptions import BaseAppException
from config.logging import init_logging
from ingestion.event_fetcher import fetch_events
from ingestion.factory import get_default_provider
from ingestion.fetcher import fetch_ticker
from schemas.responses import ApiResponse
from storage.filesystem import save_csv
from storage.naming import raw_data_path, raw_event_path


class TimeRange(str, Enum):
    ONE_DAY = "1d"
    FIVE_DAYS = "5d"
    ONE_MONTH = "1mo"
    THREE_MONTHS = "3mo"
    SIX_MONTHS = "6mo"
    ONE_YEAR = "1y"
    TWO_YEARS = "2y"
    FIVE_YEARS = "5y"
    TEN_YEARS = "10y"
    YTD = "ytd"
    MAX = "max"


class EventType(str, Enum):
    DIVIDENDS = "dividends"
    SPLITS = "splits"
    ACTIONS = "actions"


app = FastAPI(title="Market Data Pipeline API")

logger = init_logging()
logger.info("API initialized")

metrics_registry = MetricsRegistry()


@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start

    route = request.scope.get("route")
    # Record the route template, not the raw path, to keep cardinality bounded.
    path = route.path if route is not None else UNMATCHED_ROUTE
    if path != "/metrics":
        metrics_registry.record(request.method, path, response.status_code, duration)

    return response


@app.get("/health")
def health():
    return ApiResponse(
        status=200,
        message="API is healthy",
    )


@app.get("/metrics")
def metrics():
    return ApiResponse(status=200, data=metrics_registry.snapshot())


@app.exception_handler(BaseAppException)
async def base_app_exception_handler(request: Request, exc: BaseAppException):
    response = ApiResponse(status=exc.status_code, message=exc.message, data=None)

    return JSONResponse(status_code=exc.status_code, content=response.model_dump())


@app.get("/ticker/{ticker_symbol}/{time_range}", response_model=ApiResponse)
def ticker(ticker_symbol: str, time_range: TimeRange):
    was_cached = (
        get_default_provider().peek_history(ticker_symbol, time_range) is not None
    )

    data = fetch_ticker(ticker_symbol, time_range)
    logger.info(
        "Fetched ticker data",
        extra={
            "ticker_symbol": ticker_symbol,
            "time_range": time_range,
            "rows": len(data),
            "cached": was_cached,
        },
    )

    response_data = {
        "ticker": ticker_symbol,
        "rows": len(data),
        "cached": was_cached,
    }

    if not was_cached:
        path = raw_data_path(ticker_symbol, time_range)
        save_csv(path, data)
        logger.info("Stored ticker data", extra={"file_path": path})
        response_data["file_path"] = str(path)

    return ApiResponse(status=200, data=response_data)


@app.get("/events/{ticker_symbol}/{event_type}", response_model=ApiResponse)
def event(
    ticker_symbol: str,
    event_type: EventType,
    start: str | None = None,
    end: str | None = None,
):
    was_cached = (
        get_default_provider().peek_events(ticker_symbol, event_type) is not None
    )

    events = fetch_events(ticker_symbol, event_type, start=start, end=end)
    logger.info(
        "Fetched events",
        extra={
            "ticker_symbol": ticker_symbol,
            "event_type": event_type,
            "events": len(events),
            "cached": was_cached,
        },
    )

    response_data = {
        "ticker": ticker_symbol,
        "event_type": event_type,
        "events": len(events),
        "start": start,
        "end": end,
        "cached": was_cached,
    }

    if not was_cached:
        path = raw_event_path(ticker_symbol, event_type)
        save_csv(path, events)
        logger.info("Stored events", extra={"file_path": path})
        response_data["file_path"] = str(path)

    return ApiResponse(status=200, data=response_data)
