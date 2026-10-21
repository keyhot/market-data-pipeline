"""The FastAPI app: lifespan, middleware, exception handler, static mount and
the routers (#44).

The routes themselves live in `api/routers/` (one module per group); the
process-wide singletons (`scheduler_service`, `metrics_registry`, `logger`) are in
`api/shared.py`, imported here so `from api.main import app, scheduler_service`
and the Dockerfile's `api.main:app` keep working. This module imports the
routers; none of them imports this one.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.metrics import UNMATCHED_ROUTE
from api.routers import charts, market, ops, sse, stored, world
from api.shared import logger, metrics_registry, scheduler_service
from config.exceptions import BaseAppException
from ingestion.binance_ws import (
    BinanceKlineIngester,
    crypto_watchlist_symbols,
    ws_ingest_enabled,
)
from scheduler.service import scheduler_enabled
from scheduler.watchlist import load_watchlist
from schemas.responses import ApiResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    if scheduler_enabled():
        scheduler_service.start()
    ws_task: asyncio.Task | None = None
    if ws_ingest_enabled():
        symbols = crypto_watchlist_symbols(load_watchlist())
        if symbols:
            ingester = BinanceKlineIngester(symbols)
            ws_task = asyncio.create_task(ingester.run())
    yield
    if ws_task is not None:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
    scheduler_service.shutdown()


app = FastAPI(title="Market Data Pipeline API", lifespan=lifespan)

logger.info("API initialized")


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


# The renderer libraries (PixiJS, Lightweight Charts) are vendored here and
# served same-origin rather than pulled from a CDN at page load. A CDN is a
# third party in the ON-AIR path: on 2026-08-24 a transient unpkg edge response
# arrived without an `Access-Control-Allow-Origin` header, the `crossorigin`
# fetch that SRI requires was rejected, `PIXI` stayed undefined, and /world
# showed its "renderer unavailable" card on the live stream for two hours while
# OBS reported streaming=true and a 0.007% drop ratio. Same-origin means the
# only thing that can fail is the API that is already serving the page.
_STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# Route groups. Registered after the static mount, as the routes always were;
# no two of them can match the same URL, so registration order does not decide
# a request (tests/api/test_route_inventory.py pins the URL -> route table).
for _router in (ops, market, stored, sse, world, charts):
    app.include_router(_router.router)


@app.exception_handler(BaseAppException)
async def base_app_exception_handler(request: Request, exc: BaseAppException):
    response = ApiResponse(status=exc.status_code, message=exc.message, data=None)

    return JSONResponse(status_code=exc.status_code, content=response.model_dump())
