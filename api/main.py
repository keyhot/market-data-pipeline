import asyncio
import time
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from api.metrics import UNMATCHED_ROUTE, MetricsRegistry
from config.exceptions import BaseAppException, NoDataFoundError
from config.logging import init_logging
from ingestion.event_fetcher import fetch_events
from ingestion.factory import get_default_provider
from ingestion.fetcher import fetch_ticker_async
from ingestion.news_fetcher import fetch_news
from scheduler.service import SchedulerService, scheduler_enabled
from schemas.enums import EventType, TimeRange
from schemas.responses import ApiResponse
from storage.filesystem import save_csv
from storage.naming import raw_data_path, raw_event_path
from storage.news_store import CsvNewsStore
from storage.postgres_store import BAR_INTERVAL, get_price_bars
from storage.writes import (
    postgres_status,
    write_events,
    write_metrics,
    write_news,
    write_price_bars,
)

scheduler_service = SchedulerService()
news_store = CsvNewsStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if scheduler_enabled():
        scheduler_service.start()
    yield
    scheduler_service.shutdown()


app = FastAPI(title="Market Data Pipeline API", lifespan=lifespan)

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
        data={
            "scheduler": scheduler_service.status(),
            "postgres": postgres_status(),
        },
    )


@app.get("/metrics")
def metrics():
    return ApiResponse(
        status=200,
        data={
            **metrics_registry.snapshot(),
            "scheduler": scheduler_service.status(),
            "postgres_writes": write_metrics(),
        },
    )


@app.exception_handler(BaseAppException)
async def base_app_exception_handler(request: Request, exc: BaseAppException):
    response = ApiResponse(status=exc.status_code, message=exc.message, data=None)

    return JSONResponse(status_code=exc.status_code, content=response.model_dump())


MAX_BATCH_SYMBOLS = 10


async def _fetch_and_store_ticker(ticker_symbol: str, time_range: TimeRange) -> dict:
    was_cached = (
        get_default_provider().peek_history(ticker_symbol, time_range) is not None
    )

    data = await fetch_ticker_async(ticker_symbol, time_range)
    logger.info(
        "Fetched ticker data",
        extra={
            "ticker_symbol": ticker_symbol,
            "time_range": time_range,
            "rows": len(data),
            "cached": was_cached,
        },
    )

    result = {
        "ticker": ticker_symbol,
        "rows": len(data),
        "cached": was_cached,
    }

    if not was_cached:
        write_price_bars(ticker_symbol, data)
        path = raw_data_path(ticker_symbol, time_range)
        save_csv(path, data)
        logger.info("Stored ticker data", extra={"file_path": path})
        result["file_path"] = str(path)

    return result


@app.get("/ticker/{ticker_symbol}/{time_range}", response_model=ApiResponse)
async def ticker(ticker_symbol: str, time_range: TimeRange):
    response_data = await _fetch_and_store_ticker(ticker_symbol, time_range)

    return ApiResponse(status=200, data=response_data)


@app.get("/tickers/{time_range}", response_model=ApiResponse)
async def tickers(time_range: TimeRange, symbols: str):
    symbol_list = list(
        dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip())
    )

    if not symbol_list:
        raise BaseAppException("No symbols provided", status_code=400)

    if len(symbol_list) > MAX_BATCH_SYMBOLS:
        raise BaseAppException(
            f"Too many symbols: {len(symbol_list)} exceeds "
            f"the {MAX_BATCH_SYMBOLS} symbol limit",
            status_code=400,
        )

    outcomes = await asyncio.gather(
        *(_fetch_and_store_ticker(symbol, time_range) for symbol in symbol_list),
        return_exceptions=True,
    )

    results = {}
    succeeded = 0
    for symbol, outcome in zip(symbol_list, outcomes):
        if isinstance(outcome, BaseAppException):
            results[symbol] = {"error": outcome.message, "status": outcome.status_code}
        elif isinstance(outcome, BaseException):
            logger.error(
                "Unexpected error fetching symbol in batch",
                extra={"ticker_symbol": symbol, "error": repr(outcome)},
            )
            results[symbol] = {"error": "Internal error", "status": 500}
        else:
            results[symbol] = outcome
            succeeded += 1

    return ApiResponse(
        status=200,
        data={
            "time_range": time_range,
            "requested": len(symbol_list),
            "succeeded": succeeded,
            "failed": len(symbol_list) - succeeded,
            "results": results,
        },
    )


@app.get("/news/{ticker_symbol}", response_model=ApiResponse)
def news(
    ticker_symbol: str,
    limit: int = Query(10, ge=1, le=100),
    since: str | None = None,
):
    was_cached = get_default_provider().peek_news(ticker_symbol) is not None

    items = fetch_news(ticker_symbol, limit=limit, since=since)
    logger.info(
        "Fetched news",
        extra={
            "ticker_symbol": ticker_symbol,
            "news_items": len(items),
            "cached": was_cached,
        },
    )

    response_data = {
        "ticker": ticker_symbol,
        "count": len(items),
        "since": since,
        "cached": was_cached,
        "items": items.assign(
            published_at=items["published_at"].map(
                lambda ts: ts.isoformat() if pd.notna(ts) else None
            )
        ).to_dict(orient="records"),
    }

    if not was_cached:
        write_news(ticker_symbol, items)
        path = news_store.save(ticker_symbol, items)
        logger.info("Stored news", extra={"file_path": path})
        response_data["file_path"] = path

    return ApiResponse(status=200, data=response_data)


@app.get("/bars/{ticker_symbol}", response_model=ApiResponse)
def bars(
    ticker_symbol: str,
    interval: str = BAR_INTERVAL,
    limit: int = Query(100, ge=1, le=1000),
):
    try:
        stored_bars = get_price_bars(ticker_symbol, interval=interval, limit=limit)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    if not stored_bars:
        raise NoDataFoundError("No bars stored for the given parameters")

    return ApiResponse(
        status=200,
        data={
            "ticker": ticker_symbol.upper(),
            "interval": interval,
            "count": len(stored_bars),
            "bars": stored_bars,
        },
    )


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
        write_events(ticker_symbol, event_type, events)
        path = raw_event_path(ticker_symbol, event_type)
        save_csv(path, events)
        logger.info("Stored events", extra={"file_path": path})
        response_data["file_path"] = str(path)

    return ApiResponse(status=200, data=response_data)
