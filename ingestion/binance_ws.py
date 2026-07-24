"""Binance websocket kline ingester (Sprint 8).

Streams 1m klines for the watchlist's crypto symbols and writes **closed**
candles into Postgres through the mandatory write path. Reconnects with
exponential backoff; on (re)connect it REST-backfills the gap since the last
stored 1m bar, so restarts never lose candles — every write is an idempotent
upsert, so overlap is harmless.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import pandas as pd
import websockets

from ingestion.binance_provider import BinanceProvider, _klines_to_frame
from storage.postgres_store import latest_bar_timestamp
from storage.writes import postgres_write_enabled, write_price_bars

WS_INGEST_ENABLED_ENV = "WS_INGEST_ENABLED"
WS_BASE_URL = "wss://stream.binance.com:9443/stream"

INTRADAY_INTERVAL = "1m"
_BACKOFF_INITIAL_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0

logger = logging.getLogger(__name__)


def ws_ingest_enabled() -> bool:
    raw = os.environ.get(WS_INGEST_ENABLED_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes"}


class BinanceKlineIngester:
    def __init__(
        self,
        symbols: list[str],
        provider: BinanceProvider | None = None,
        ws_base_url: str = WS_BASE_URL,
    ):
        self._symbols = [s.upper() for s in symbols]
        self._provider = provider or BinanceProvider()
        self._ws_base_url = ws_base_url
        self.bars_written = 0

    @property
    def stream_url(self) -> str:
        streams = "/".join(
            f"{s.lower()}@kline_{INTRADAY_INTERVAL}" for s in self._symbols
        )
        return f"{self._ws_base_url}?streams={streams}"

    async def run(self) -> None:
        """Connect-consume loop; runs until cancelled."""
        backoff = _BACKOFF_INITIAL_SECONDS
        while True:
            try:
                await asyncio.to_thread(self.backfill_gaps)
                async with websockets.connect(
                    self.stream_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    logger.info(
                        "Kline stream connected", extra={"symbols": self._symbols}
                    )
                    backoff = _BACKOFF_INITIAL_SECONDS
                    async for message in ws:
                        await asyncio.to_thread(self.handle_message, message)
                # The async-for ends without raising on a *clean* server close
                # (Binance recycles connections ~daily). Fall through to the same
                # backoff sleep as a drop — otherwise this busy-loops with zero
                # delay, hammering the REST backfill + WS endpoints.
                logger.info(
                    "Kline stream closed cleanly, reconnecting",
                    extra={"backoff_seconds": backoff},
                )
            except asyncio.CancelledError:
                logger.info("Kline ingester stopped")
                raise
            except Exception as e:
                logger.warning(
                    "Kline stream dropped, reconnecting",
                    extra={"error": str(e), "backoff_seconds": backoff},
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    def handle_message(self, message: str | bytes) -> None:
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            logger.warning("Unparseable kline message dropped")
            return
        kline = (payload.get("data") or {}).get("k") or {}
        # Only closed candles are stored; in-progress ones change every tick.
        if not kline.get("x"):
            return
        symbol = str(kline.get("s", "")).upper()
        if symbol not in self._symbols:
            return
        frame = pd.DataFrame(
            {
                "Open": [float(kline["o"])],
                "High": [float(kline["h"])],
                "Low": [float(kline["l"])],
                "Close": [float(kline["c"])],
                "Volume": [float(kline["v"])],
            },
            index=pd.to_datetime([kline["t"]], unit="ms", utc=True),
        )
        write_price_bars(symbol, frame, interval=INTRADAY_INTERVAL)
        self.bars_written += 1

    def backfill_gaps(self) -> None:
        """REST-fill 1m bars missed while disconnected (or ever)."""
        if not postgres_write_enabled():
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # The candle opening at the current minute is still forming; anything
        # opening before it is closed. Filtering on open time (not position)
        # is robust even when a page caps at 1000 rows.
        current_minute_ms = (now_ms // 60_000) * 60_000
        for symbol in self._symbols:
            try:
                last = latest_bar_timestamp(symbol, INTRADAY_INTERVAL)
                if last is not None:
                    # Paginate the whole gap. A single 1000-candle request would
                    # silently drop everything past ~16.7h and leave a PERMANENT
                    # hole: the next reconnect advances latest_bar_timestamp past
                    # the gap and never revisits it.
                    start_ms = int(last.timestamp() * 1000) + 60_000
                    raw = self._provider.get_klines_paginated(
                        symbol, interval=INTRADAY_INTERVAL,
                        start_ms=start_ms, end_ms=current_minute_ms,
                    )
                else:
                    # Cold start: seed the most recent window; the deep historical
                    # fill is a separate script's job, not the ingester's.
                    raw = self._provider.get_klines(
                        symbol, interval=INTRADAY_INTERVAL
                    )
                closed = [k for k in raw if k[0] < current_minute_ms]
                if not closed:
                    continue
                frame = _klines_to_frame(closed)
                write_price_bars(symbol, frame, interval=INTRADAY_INTERVAL)
                self.bars_written += len(frame)
                logger.info(
                    "Backfilled kline gap",
                    extra={"symbol": symbol, "bars": len(frame)},
                )
            except Exception as e:
                logger.warning(
                    "Kline backfill failed",
                    extra={"symbol": symbol, "error": str(e)},
                )


def crypto_watchlist_symbols(watchlist) -> list[str]:
    """Unique crypto symbols from a loaded Watchlist, order preserved."""
    seen: dict[str, None] = {}
    for spec in watchlist.tickers:
        if spec.market == "crypto":
            seen.setdefault(spec.symbol, None)
    return list(seen)
