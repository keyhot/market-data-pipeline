"""Server-Sent Events: new bars and new world events, by polling Postgres
(#44, moved from `api/main.py`)."""

import asyncio
import json

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from api.shared import _validated_symbol
from storage.postgres_store import BAR_INTERVAL, get_price_bars, get_world_events

router = APIRouter()


@router.get("/stream/bars/{ticker_symbol}")
async def stream_bars(
    ticker_symbol: str,
    interval: str = BAR_INTERVAL,
    poll_seconds: float = Query(3.0, ge=0.5, le=60.0),
):
    """Server-Sent Events: emits each bar stored after the client connected.
    Polls Postgres — works across containers, and EventSource auto-reconnects."""
    symbol = _validated_symbol(ticker_symbol)
    return StreamingResponse(
        _bar_event_stream(symbol, interval, poll_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _bar_event_stream(symbol: str, interval: str, poll_seconds: float):
    last_ts: str | None = None
    while True:
        try:
            stored = await asyncio.to_thread(get_price_bars, symbol, interval, 10)
        except Exception:
            stored = []  # transient DB failure: keep the stream alive
        if stored:
            if last_ts is None:
                # Bars from before the connect; the page fetched those via /bars.
                last_ts = stored[-1]["timestamp"]
            else:
                for bar in stored:
                    if bar["timestamp"] > last_ts:
                        last_ts = bar["timestamp"]
                        yield f"data: {json.dumps(bar)}\n\n"
        yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)


@router.get("/stream/world/events")
async def stream_world_events(
    poll_seconds: float = Query(3.0, ge=0.5, le=60.0),
):
    """SSE: emits world events stored after the client connected."""
    return StreamingResponse(
        _world_event_stream(poll_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _world_event_stream(poll_seconds: float):
    last_id: int | None = None
    while True:
        try:
            stored = await asyncio.to_thread(get_world_events, 20)
        except Exception:
            stored = []  # transient DB failure: keep the stream alive
        if stored:
            newest_first = stored  # reader returns newest first
            if last_id is None:
                # Events from before the connect; the page fetched those
                # via /world/events.
                last_id = newest_first[0]["id"]
            else:
                fresh = [e for e in newest_first if e["id"] > last_id]
                for event in reversed(fresh):  # emit oldest first
                    last_id = max(last_id, event["id"])
                    yield f"data: {json.dumps(event, default=str)}\n\n"
        yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)
