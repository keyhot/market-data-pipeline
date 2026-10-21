"""Reads of what Postgres already holds: bars, stored events/news, signals
(#44, moved from `api/main.py`)."""

from fastapi import APIRouter, Query

from api.shared import _validated_symbol
from config.exceptions import BaseAppException, NoDataFoundError
from schemas.enums import EventType
from schemas.responses import ApiResponse
from storage.postgres_store import (
    BAR_INTERVAL,
    get_corporate_events,
    get_news_items,
    get_price_bars,
    get_signal_accuracy,
    get_signals,
)

router = APIRouter()


@router.get("/bars/{ticker_symbol}", response_model=ApiResponse)
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


@router.get("/stored/events/{ticker_symbol}/{event_type}", response_model=ApiResponse)
def stored_events(
    ticker_symbol: str,
    event_type: EventType,
    limit: int = Query(100, ge=1, le=1000),
):
    stored_type = None if event_type == EventType.ACTIONS else str(event_type)
    try:
        events = get_corporate_events(
            ticker_symbol, event_type=stored_type, limit=limit
        )
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    if not events:
        raise NoDataFoundError("No events stored for the given parameters")

    return ApiResponse(
        status=200,
        data={
            "ticker": ticker_symbol.upper(),
            "event_type": event_type,
            "count": len(events),
            "events": events,
        },
    )


@router.get("/stored/news/{ticker_symbol}", response_model=ApiResponse)
def stored_news(ticker_symbol: str, limit: int = Query(20, ge=1, le=100)):
    try:
        items = get_news_items(ticker_symbol, limit=limit)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    if not items:
        raise NoDataFoundError("No news stored for the given parameters")

    return ApiResponse(
        status=200,
        data={
            "ticker": ticker_symbol.upper(),
            "count": len(items),
            "items": items,
        },
    )


@router.get("/signals/{ticker_symbol}", response_model=ApiResponse)
def signals(
    ticker_symbol: str,
    interval: str = "1m",
    limit: int = Query(50, ge=1, le=500),
):
    symbol = _validated_symbol(ticker_symbol)
    try:
        stored = get_signals(symbol, interval, limit)
        accuracy = get_signal_accuracy(symbol, interval)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)
    if not stored:
        raise NoDataFoundError("No signals stored for the given parameters")
    return ApiResponse(
        status=200,
        data={
            "ticker": symbol,
            "interval": interval,
            "count": len(stored),
            "signals": stored,
            "accuracy": accuracy,
        },
    )
