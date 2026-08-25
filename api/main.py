import asyncio
import html
import json
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.metrics import UNMATCHED_ROUTE, MetricsRegistry
from config.exceptions import BaseAppException, NoDataFoundError
from config.logging import init_logging
from ingestion.binance_ws import (
    BinanceKlineIngester,
    crypto_watchlist_symbols,
    ws_ingest_enabled,
)
from ingestion.event_fetcher import fetch_events
from ingestion.factory import get_default_provider
from ingestion.fetcher import fetch_ticker_async
from ingestion.news_fetcher import fetch_news
from scheduler.service import SchedulerService, scheduler_enabled
from scheduler.watchlist import load_watchlist
from schemas.enums import EventType, TimeRange
from schemas.responses import ApiResponse
from storage.filesystem import csv_write_enabled, save_csv
from storage.naming import raw_data_path, raw_event_path
from storage.news_store import CsvNewsStore
from storage.postgres_store import (
    BAR_INTERVAL,
    get_corporate_events,
    get_latest_closes,
    get_model_accuracy,
    get_news_items,
    get_now_playing,
    get_price_bars,
    get_signal_accuracy,
    get_signals,
    get_world_events,
)
from storage.writes import (
    postgres_status,
    write_events,
    write_metrics,
    write_news,
    write_price_bars,
)
from world import reactions, visuals
from world.reactions import attach_reactions
from world.renderer_health import record_beat, renderer_status
from world.salience import KNOWN_EVENT_TYPES
from world.state import GENERIC_TIER_CUTS, project_state, tier_cuts, tier_of_js

scheduler_service = SchedulerService()
news_store = CsvNewsStore()

# KI-046: registry behind the renderer heartbeat. `record_beat`/`renderer_status`
# (world.renderer_health) are pure; this dict plus the lock are the only state
# and I/O around them. `/health` is a sync `def`, so FastAPI runs it — and
# `world_heartbeat` — in a threadpool: several OBS browser sources hit the
# heartbeat route concurrently, and `renderer_status` prunes (deletes) the
# store while iterating it, so unsynchronized access can race.
_RENDERER_BEATS: dict = {}
_RENDERER_BEATS_LOCK = threading.Lock()
# Captured at import, not in `lifespan`: Starlette only runs lifespan on
# context-manager entry, and this task's tests construct TestClient(app) at
# module level, so a lifespan-set value would be unset in every test.
_PROCESS_STARTED_AT = time.monotonic()


class RendererBeat(BaseModel):
    page: str
    frames: int


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


_TEMPLATES_DIR = Path(__file__).parent / "templates"
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
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,15}$")


def _validated_symbol(raw: str) -> str:
    symbol = raw.upper()
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise BaseAppException(f"Invalid symbol: {raw!r}", status_code=400)
    return symbol


# The shared visual identity (world/visuals.py) reaches every page from one
# place: the palette vars, the calm->dramatic tier ramp, and the mood->colour
# map the canvas reads. Injecting them here means a page can never drift from
# the source of truth, and no caller can forget to pass them.
_THEME_REPLACEMENTS = {
    "__THEME_VARS__": visuals.css_variables(),
    "__TIER_STYLES__": visuals.tier_styles_css(),
    "__MOOD_COLORS_JSON__": json.dumps(visuals.MOOD_COLORS),
    # KI-028: the cast measured 1.19:1 and 1.00:1 against its own background.
    # Body colour is not the mood colour — PixiJS tint MULTIPLIES the base fill
    # — so the page cannot work it out from __MOOD_COLORS_JSON__ alone, and the
    # old attempt to (a luminance floor applied to the tint) was floored on the
    # wrong number. Precomputed per mood here, where the contrast maths and the
    # base fill live together.
    "__BODY_TINTS_JSON__": json.dumps(visuals.body_tints()),
    "__BODY_BASE_FILL__": hex(visuals.BODY_BASE_FILL),
    "__BODY_RIM_FILL__": hex(visuals.BODY_RIM_FILL),
    # B2: scene-wide lighting per tier, from the same module as everything else
    # visual. The canvas gets a ramp shaped for a canvas — see `room_light`.
    "__ROOM_LIGHT_JSON__": json.dumps(
        [visuals.room_light(tier) for tier in range(4)]
    ),
    # The 0..3 tier scale, from world.state. Severities are rule-specific, so a
    # page that hard-codes absolute cuts disagrees with the server about how
    # big an event is — which is how every `signal_resolved` came out tier 0.
    "__TIER_CUTS_JSON__": json.dumps(
        {
            "cuts": {k: list(v) for k, v in tier_cuts().items()},
            "generic": list(GENERIC_TIER_CUTS),
        }
    ),
    # ...and the three lines that read them, which were written out twice,
    # byte-identical, in two templates. One definition, injected — see
    # `world.state.tier_of_js`.
    "__TIER_OF_JS__": tier_of_js(),
    # B5: one accent per speaking character, from the same palette module, so
    # a speech bubble can never drift from the room's colours.
    "__CHARACTER_COLORS_JSON__": json.dumps(visuals.CHARACTER_COLORS),
    # B1: the reaction registry itself reaches the canvas, so "what happened"
    # maps to a face and a named animation from ONE source of truth. The
    # renderer must not re-invent this mapping — drift between the room and the
    # overlays was the failure world/visuals.py exists to prevent.
    "__REACTIONS_JSON__": json.dumps(
        {
            "base": {
                event_type: {"mood": mood, "animation": animation}
                for event_type, (mood, animation) in reactions.REACTIONS.items()
            },
            "outcomes": {
                outcome: {"mood": mood, "animation": animation}
                for outcome, (mood, animation) in reactions._SIGNAL_OUTCOMES.items()
            },
            "directions": {
                direction: {"mood": mood, "animation": animation}
                for direction, (mood, animation) in reactions._STREAK_DIRECTIONS.items()
            },
            "fallback": {
                "mood": reactions._FALLBACK[0],
                "animation": reactions._FALLBACK[1],
            },
        }
    ),
}


def _render_template(name: str, replacements: dict[str, str]) -> str:
    html = (_TEMPLATES_DIR / name).read_text()
    # Page-specific replacements win over theme defaults if a key collides.
    for placeholder, value in {**_THEME_REPLACEMENTS, **replacements}.items():
        html = html.replace(placeholder, value)
    return html


@app.post("/world/heartbeat")
def world_heartbeat(beat: RendererBeat, request: Request):
    """KI-046: the page says it is alive, and carries the frame count that
    stops a frozen page from being able to say so convincingly.

    Keyed by Host, because OBS's sources load 127.0.0.N shards and a developer
    tab on localhost must never cover for a dead on-air source.
    """
    with _RENDERER_BEATS_LOCK:
        record_beat(
            _RENDERER_BEATS,
            host=request.headers.get("host", "unknown"),
            page=beat.page,
            frames=beat.frames,
            now=time.monotonic(),
        )
    return ApiResponse(status=200, message="beat recorded", data={})


@app.get("/health")
def health():
    with _RENDERER_BEATS_LOCK:
        renderer = renderer_status(
            _RENDERER_BEATS, now=time.monotonic(), started_at=_PROCESS_STARTED_AT
        )
    return ApiResponse(
        status=200,
        message="API is healthy",
        data={
            "scheduler": scheduler_service.status(),
            "postgres": postgres_status(),
            # `healthy` here folds EVERY page that posted, including developer
            # tabs, so it is advisory. The watchdog judges one host out of
            # `pages` — see Task 15. The API cannot know which source is on air.
            "renderer": renderer,
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
        if csv_write_enabled():
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
        if csv_write_enabled():
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


@app.get("/stored/events/{ticker_symbol}/{event_type}", response_model=ApiResponse)
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


@app.get("/stored/news/{ticker_symbol}", response_model=ApiResponse)
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


@app.get("/stream/bars/{ticker_symbol}")
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


@app.get("/world/events", response_model=ApiResponse)
def world_events(
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = None,
    symbol: str | None = None,
    since: str | None = None,
):
    if event_type is not None and event_type not in KNOWN_EVENT_TYPES:
        raise BaseAppException(
            f"Unknown event type: {event_type!r}", status_code=400
        )
    validated_symbol = _validated_symbol(symbol) if symbol else None
    since_dt = None
    if since is not None:
        try:
            since_dt = pd.Timestamp(since).to_pydatetime()
        except ValueError:
            raise BaseAppException(f"Invalid since: {since!r}", status_code=400)
    try:
        events = get_world_events(
            limit=limit,
            event_type=event_type,
            symbol=validated_symbol,
            since=since_dt,
        )
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)
    if not events:
        raise NoDataFoundError("No world events for the given parameters")
    return ApiResponse(
        status=200, data={"count": len(events), "events": events}
    )


@app.get("/world/state", response_model=ApiResponse)
def world_state(limit: int = Query(500, ge=1, le=2000)):
    """The world_events log folded into what the room currently shows.
    The renderer computes nothing — it draws this."""
    try:
        events = get_world_events(limit=limit)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)
    if not events:
        raise NoDataFoundError("No world events recorded yet")
    state = attach_reactions(project_state(events))
    # KI-012: the model's on-screen accuracy comes from the SAME last-N-resolved
    # basis as the overlay strips (not the event-window fold), so the room and the
    # strips agree. Enrichment — never let it break the state.
    predict_symbols: list[str] = []
    try:
        predict_symbols = [
            spec.symbol.upper()
            for spec in load_watchlist().tickers
            if spec.predict and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
        ]
        state["model"]["accuracy"] = get_model_accuracy(predict_symbols)
    except Exception:
        logger.warning("Could not attach model accuracy to /world/state")
    # B9: the always-on now-band needs a price. It rides along here rather than
    # the page fetching /bars per symbol — every extra request is another
    # long-lived connection in a browser process that already exhausted its
    # per-origin limit once (KI-013). Enrichment: a failure costs one line in
    # the band, never the room.
    prices: dict[str, float] = {}
    try:
        for symbol in predict_symbols:
            bars = get_price_bars(symbol, interval="1m", limit=1)
            if bars:
                prices[symbol] = bars[-1]["close"]
    except Exception:
        logger.warning("Could not attach prices to /world/state")
    state["prices"] = prices
    return ApiResponse(status=200, data=state)


@app.get("/signals/{ticker_symbol}", response_model=ApiResponse)
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


@app.get("/stream/world/events")
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


@app.get("/overlay/signals", response_class=HTMLResponse)
def overlay_signals():
    """OBS Browser Source strip (~1920x120): live signals + track record."""
    symbols = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.predict and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(symbols))
    return HTMLResponse(
        _render_template("overlay_signals.html", {"__SYMBOLS__": json.dumps(deduped)})
    )


# How long after a track should have ended we keep crediting it. The runner
# polls every 5s and advances on ENDED, so a live bed's row is never more than
# about `duration + 5s` old; anything older means the runner is gone.
_CREDIT_GRACE_SECONDS = 30.0
# Fallback when a row carries no duration — defensive only (all shipped tracks
# parse), but an unbounded credit is exactly what this guard exists to prevent.
_CREDIT_MAX_SECONDS = 600.0


def _credit_expired(started_at, duration_seconds) -> bool:
    """Has this row outlived the track it describes?

    `music_now_playing` is an upsert, so the last track the runner started
    stays in the table forever. If the bed dies — a crash loop, a stopped
    unit, OBS relaunched under it — the row is still there and the strip would
    go on crediting a song over silence. A credit nobody asked us to give,
    naming a track that is not playing, is worse than no credit at all.
    """
    if started_at is None:
        return True
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    length = duration_seconds if duration_seconds else _CREDIT_MAX_SECONDS
    return elapsed > length + _CREDIT_GRACE_SECONDS


@app.get("/music/now-playing")
def music_now_playing():
    """The bed's current track, for the credit line on the signals strip.

    Read-only and forgiving: the bed is a courtesy (neither Mixkit's nor
    Pixabay's licence requires attribution), so a missing runner, a stale row
    or a DB blip all answer `{"playing": null}` rather than an error. The strip
    then shows nothing at all, which is the correct look for a stream with no
    music.
    """
    try:
        current = get_now_playing()
    except Exception:  # noqa: BLE001 - a DB blip must not 500 the overlay
        current = None
    if not current or _credit_expired(
        current.get("started_at"), current.get("duration_seconds")
    ):
        return ApiResponse(status=200, data={"playing": None})
    return ApiResponse(
        status=200,
        data={
            "playing": {
                "title": current["title"],
                "artist": current["artist"],
                "source": current["source"],
                "source_url": current["source_url"],
                "license": current["license"],
                "started_at": current["started_at"],
                "duration_seconds": current["duration_seconds"],
            }
        },
    )


@app.get("/overlay/events", response_class=HTMLResponse)
def overlay_events():
    """OBS Browser Source feed (~480x1080): latest salient world events."""
    return HTMLResponse(_render_template("overlay_events.html", {}))


# B10. What a viewer sees instead of a frozen or black frame while the watchdog
# brings the stream back. Copy only — the card is otherwise identical in every
# state, so a switch between them never reads as a different channel.
_STANDBY_COPY = {
    "reconnecting": (
        "Reconnecting",
        "The stream dropped and is coming back on its own",
    ),
    "starting": ("Starting soon", "Warming up the market feeds"),
    "shortly": ("Back shortly", "Taking a moment to sort something out"),
}
_STANDBY_DEFAULT = "reconnecting"


@app.get("/standby", response_class=HTMLResponse)
def standby_page(state: str = _STANDBY_DEFAULT):
    """Procedural standby card for the `standby` OBS scene (B10).

    Unknown states fall back rather than erroring: this page is the surface
    shown *because* something already went wrong, so it must never be the thing
    that breaks. The state is escaped, never trusted — it reaches the DOM as
    text through a template substitution, the same rule as every other page.
    """
    headline, subline = _STANDBY_COPY.get(state, _STANDBY_COPY[_STANDBY_DEFAULT])
    if state not in _STANDBY_COPY:
        logger.warning("Unknown standby state requested", extra={"state": state})
    return HTMLResponse(
        _render_template(
            "standby.html",
            {"__HEADLINE__": html.escape(headline),
             "__SUBLINE__": html.escape(subline)},
        )
    )


@app.get("/world", response_class=HTMLResponse)
def world_page():
    """The Living World room — a Browser Source for the world-focus scene."""
    symbols = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.market == "crypto" and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(symbols))
    return HTMLResponse(
        _render_template("world.html", {"__SYMBOLS__": json.dumps(deduped)})
    )


_ALLOWED_CHART_INTERVALS = {"1d", "1m"}


@app.get("/chart/{ticker_symbol}", response_class=HTMLResponse)
def chart(ticker_symbol: str, interval: str = BAR_INTERVAL):
    symbol = _validated_symbol(ticker_symbol)
    if interval not in _ALLOWED_CHART_INTERVALS:
        raise BaseAppException(f"Invalid interval: {interval!r}", status_code=400)
    return HTMLResponse(
        _render_template(
            "chart.html", {"__SYMBOL__": symbol, "__INTERVAL__": interval}
        )
    )


@app.get("/charts", response_class=HTMLResponse)
def charts(interval: str = BAR_INTERVAL, symbols: str | None = None):
    """Multi-symbol chart page (chart-focus scene): one candlestick panel per
    watchlist `predict` symbol — BTC *and* ETH — each with prediction markers.

    `?symbols=BTCUSDT` narrows it to a subset. That exists so a scene needing a
    single-symbol chart uses this chrome-free broadcast surface instead of
    `/chart/{symbol}`, whose nav link, status line and footer are meant for a
    human with a browser and went out on air until KI-027."""
    if interval not in _ALLOWED_CHART_INTERVALS:
        raise BaseAppException(f"Invalid interval: {interval!r}", status_code=400)
    available = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.predict and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(available))
    if symbols is not None:
        # Filtered against the watchlist, never taken from the query string:
        # these are rendered into the page's JS.
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in deduped]
        if not wanted or unknown:
            raise BaseAppException(
                f"Unknown chart symbol(s): {', '.join(unknown) or symbols!r}",
                status_code=400,
            )
        deduped = [s for s in deduped if s in wanted]
    return HTMLResponse(
        _render_template(
            "charts.html",
            {"__SYMBOLS__": json.dumps(deduped), "__INTERVAL__": interval},
        )
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    deduped = dict.fromkeys(spec.symbol.upper() for spec in load_watchlist().tickers)
    symbols = []
    for raw in deduped:
        if _SYMBOL_PATTERN.fullmatch(raw):
            symbols.append(raw)
        else:
            logger.warning("Skipping invalid watchlist symbol", extra={"symbol": raw})
    try:
        closes = {row["symbol"]: row for row in get_latest_closes(symbols)}
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    rows = []
    for symbol in symbols:
        row = closes.get(symbol)
        close = f"{row['close']:.2f}" if row and row["close"] is not None else "—"
        as_of = row["timestamp"][:10] if row else "—"
        rows.append(
            f'      <tr><td><a href="/chart/{symbol}">{symbol}</a></td>'
            f'<td class="num">{close}</td><td>{as_of}</td></tr>'
        )
    return HTMLResponse(
        _render_template("dashboard.html", {"__ROWS__": "\n".join(rows)})
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
        if csv_write_enabled():
            path = raw_event_path(ticker_symbol, event_type)
            save_csv(path, events)
            logger.info("Stored events", extra={"file_path": path})
            response_data["file_path"] = str(path)

    return ApiResponse(status=200, data=response_data)
