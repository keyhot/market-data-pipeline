"""The liquidations recorder's runner (C1): sockets, clock and Postgres.

Three venues, one process:

- **Binance** `!forceOrder@arr` on the `/market/ws` path. The legacy `/ws`
  path went silent on 2026-09-29 while still accepting connections, which is
  why the URL is configurable and why `CoverageTracker` won't release a
  minute until a connection has delivered data.
- **OKX** public `liquidation-orders` (all SWAP). OKX closes a public socket
  after 30s without traffic and answers a text `ping` with `pong`.
  Its REST window (the latest ≤100 fills per underlying, ~1h for BTC) is
  polled for BTC and ETH as a completeness audit; those rows are marked
  `source='rest'`, are dropped on the natural key when the WS already has
  them, and never create coverage.
- **Bybit** `allLiquidation.{symbol}` for BTCUSDT and ETHUSDT: the one feed
  that claims *every* liquidation (500ms batches, since 2025-02). No
  all-market topic exists, so it is per symbol. Its app-level `{"op":"ping"}`
  is answered on the socket.

A server-acknowledged subscription (OKX, Bybit) counts as proof the feed is
flowing; Binance has no ack, so it must deliver a liquidation first.

Parsing and the coverage rule live in `ingestion/liquidations.py` (pure,
tested); this module only wires them to the network. Every write goes through
`Buffer`, which keeps rows through a Postgres outage and writes a minute's
coverage in the same transaction as the rows received in it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable

import websockets

from ingestion.liquidations import (
    BINANCE,
    BYBIT,
    OKX,
    CoverageTracker,
    Liquidation,
    OkxContract,
    okx_contracts,
    parse_binance,
    parse_bybit,
    parse_okx,
    parse_okx_data,
)

LIQUIDATION_RECORDER_ENABLED_ENV = "LIQUIDATION_RECORDER_ENABLED"
BINANCE_URL_ENV = "LIQ_BINANCE_WS_URL"
BINANCE_WS_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
OKX_REST_BASE = "https://www.okx.com"
OKX_SUBSCRIBE = json.dumps(
    {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}
)
OKX_REST_UNDERLYINGS = ("BTC-USDT", "ETH-USDT")
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
BYBIT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
BYBIT_SUBSCRIBE = json.dumps(
    {"op": "subscribe", "args": [f"allLiquidation.{s}" for s in BYBIT_SYMBOLS]}
)
BYBIT_PING = json.dumps({"op": "ping"})

PING_EVERY_S = 10.0
PONG_TIMEOUT_S = 10.0
# Two missed pings is a dead socket: reconnect rather than sit on it.
STALE_AFTER_S = 3 * PING_EVERY_S
FLUSH_EVERY_S = 5.0
REST_POLL_EVERY_S = 300.0
CONTRACTS_REFRESH_S = 6 * 3600.0
# ~2 days of the whole-market Binance feed; beyond that, drop and say so.
MAX_BUFFERED_ROWS = 200_000
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 60.0
# OKX's CDN refuses Python's default urllib User-Agent with a 403.
_HTTP_HEADERS = {"User-Agent": "market-data-pipeline/liquidation-recorder"}

logger = logging.getLogger(__name__)


def recorder_enabled() -> bool:
    raw = os.environ.get(LIQUIDATION_RECORDER_ENABLED_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes"}


def _get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class Buffer:
    """Rows waiting for Postgres, in receive order.

    Only the event loop touches the lists: `take()` hands a batch to the
    writer thread and empties the buffer, and a failed batch is `restore()`d
    in front of whatever arrived meanwhile. (Sharing the lists with the
    writer thread would delete rows that arrived mid-write, unwritten.)

    Overflow drops the oldest rows AND every pending coverage minute: a
    minute can't be claimed as covered once rows received in it may be gone.
    """

    def __init__(self, max_rows: int = MAX_BUFFERED_ROWS):
        self._max_rows = max_rows
        self.liquidations: list[Liquidation] = []
        self.coverage: list[tuple[str, int, int]] = []
        self.rows_written = 0
        self.rows_dropped = 0

    def add(self, rows: list[Liquidation]) -> None:
        self.liquidations.extend(rows)
        self._trim()

    def cover(self, venue: str, minutes: list[tuple[int, int]]) -> None:
        self.coverage.extend((venue, m, n) for m, n in minutes)

    def take(self) -> tuple[list[Liquidation], list[tuple[str, int, int]]]:
        batch = (self.liquidations, self.coverage)
        self.liquidations, self.coverage = [], []
        return batch

    def restore(self, liquidations: list[Liquidation], coverage: list) -> None:
        self.liquidations = liquidations + self.liquidations
        self.coverage = coverage + self.coverage
        self._trim()

    def _trim(self) -> None:
        overflow = len(self.liquidations) - self._max_rows
        if overflow > 0:
            del self.liquidations[:overflow]
            self.coverage.clear()
            self.rows_dropped += overflow
            logger.error(
                "Liquidation buffer overflowed; oldest rows and pending coverage dropped",
                extra={"dropped": overflow},
            )


class LiquidationRecorder:
    def __init__(
        self,
        write: Callable,
        binance_url: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._write = write
        self.buffer = Buffer()
        self._binance_url = binance_url or os.environ.get(BINANCE_URL_ENV) or BINANCE_WS_URL
        self._clock = clock
        self.contracts: dict[str, OkxContract] = {}
        self.trackers = {
            BINANCE: CoverageTracker(),
            OKX: CoverageTracker(),
            BYBIT: CoverageTracker(),
        }

    # --- frame handling (sync, testable) ------------------------------------

    def on_binance_frame(self, message: str | bytes) -> None:
        now = self._clock()
        tracker = self.trackers[BINANCE]
        rows = parse_binance(message)
        tracker.proof(now)
        if rows:
            tracker.data()
            tracker.count(now, len(rows))
            self.buffer.add(rows)

    def on_okx_frame(self, message: str | bytes) -> str:
        kind, rows = parse_okx(message, self.contracts)
        return self._on_acked_frame(OKX, kind, rows)

    def on_bybit_frame(self, message: str | bytes) -> str:
        kind, rows = parse_bybit(message)
        return self._on_acked_frame(BYBIT, kind, rows)

    def _on_acked_frame(self, venue: str, kind: str, rows: list) -> str:
        """Venues that acknowledge a subscription: the ack proves it flows."""
        if kind == "error":
            return kind
        now = self._clock()
        tracker = self.trackers[venue]
        tracker.proof(now)
        if kind in ("subscribed", "data"):
            tracker.data()
        if rows:
            tracker.count(now, len(rows))
            self.buffer.add(rows)
        return kind

    def on_pong(self, venue: str) -> None:
        self.trackers[venue].proof(self._clock())

    def collect_coverage(self) -> None:
        for venue, tracker in self.trackers.items():
            minutes = tracker.confirmed()
            if minutes:
                self.buffer.cover(venue, minutes)

    def write_batch(self, liquidations: list, coverage: list) -> bool:
        """The writer-thread half of a flush: True when the batch landed."""
        if not liquidations and not coverage:
            return True
        try:
            self._write(liquidations, coverage)
        except Exception as e:
            logger.warning(
                "Liquidation write failed; keeping rows to retry",
                extra={"rows": len(liquidations), "error": f"{type(e).__name__}: {e}"},
            )
            return False
        self.buffer.rows_written += len(liquidations)
        return True

    async def flush(self) -> bool:
        self.collect_coverage()
        liquidations, coverage = self.buffer.take()
        ok = await asyncio.to_thread(self.write_batch, liquidations, coverage)
        if not ok:
            self.buffer.restore(liquidations, coverage)
        return ok

    # --- network --------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(
            self._venue_loop(BINANCE, self._binance_session),
            self._venue_loop(OKX, self._okx_session),
            self._venue_loop(BYBIT, self._bybit_session),
            self._flush_loop(),
            self._okx_rest_loop(),
        )

    async def _venue_loop(self, venue: str, session) -> None:
        backoff = _BACKOFF_INITIAL_S
        while True:
            started = self._clock()
            try:
                await session()
                logger.info("Liquidation stream closed cleanly", extra={"venue": venue})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Liquidation stream dropped, reconnecting",
                    extra={"venue": venue, "error": f"{type(e).__name__}: {e}",
                           "backoff_seconds": backoff},
                )
            finally:
                self.trackers[venue].reset()
            # A session that lived a while earned a fresh backoff; one that
            # died at once keeps doubling, so a refusing venue isn't hammered.
            if self._clock() - started > 60:
                backoff = _BACKOFF_INITIAL_S
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _binance_session(self) -> None:
        async with websockets.connect(self._binance_url, ping_interval=None) as ws:
            logger.info("Binance liquidation stream connected", extra={"url": self._binance_url})
            keepalive = asyncio.create_task(self._binance_keepalive(ws))
            try:
                async for message in ws:
                    self.on_binance_frame(message)
            finally:
                keepalive.cancel()

    async def _binance_keepalive(self, ws) -> None:
        # Protocol-level ping: Binance answers it, and the pong is the proof
        # that carries coverage through the feed's quiet stretches.
        while True:
            await asyncio.sleep(PING_EVERY_S)
            try:
                waiter = await ws.ping()
                await asyncio.wait_for(waiter, PONG_TIMEOUT_S)
            except Exception:
                await ws.close()
                return
            self.on_pong(BINANCE)

    async def _okx_session(self) -> None:
        await self._acked_session(
            OKX, OKX_WS_URL, OKX_SUBSCRIBE, "ping", self.on_okx_frame
        )

    async def _bybit_session(self) -> None:
        await self._acked_session(
            BYBIT, BYBIT_WS_URL, BYBIT_SUBSCRIBE, BYBIT_PING, self.on_bybit_frame
        )

    async def _acked_session(self, venue, url, subscribe, ping, on_frame) -> None:
        async with websockets.connect(url, ping_interval=None) as ws:
            await ws.send(subscribe)
            logger.info("Liquidation stream connected", extra={"venue": venue})
            keepalive = asyncio.create_task(self._app_keepalive(venue, ws, ping))
            try:
                async for message in ws:
                    if on_frame(message) == "error":
                        raise RuntimeError(f"{venue} refused a request: {message!r}")
            finally:
                keepalive.cancel()

    async def _app_keepalive(self, venue: str, ws, ping: str) -> None:
        # Application-level ping (OKX: text "ping"; Bybit: {"op":"ping"}). The
        # reply arrives as a frame and is counted as a proof by on_frame.
        # OKX drops a public socket after 30s without traffic.
        tracker = self.trackers[venue]
        connected_at = self._clock()
        while True:
            await asyncio.sleep(PING_EVERY_S)
            last = tracker.last_proof or connected_at
            if self._clock() - last > STALE_AFTER_S:
                await ws.close()
                return
            await ws.send(ping)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            await self.flush()

    async def _okx_rest_loop(self) -> None:
        contracts_at = 0.0
        while True:
            if self._clock() - contracts_at > CONTRACTS_REFRESH_S:
                try:
                    payload = await asyncio.to_thread(
                        _get_json, f"{OKX_REST_BASE}/api/v5/public/instruments?instType=SWAP"
                    )
                    fresh = okx_contracts(payload)
                    if fresh:
                        self.contracts = fresh
                        contracts_at = self._clock()
                except Exception as e:
                    logger.warning("OKX instruments fetch failed", extra={"error": str(e)})
            for uly in OKX_REST_UNDERLYINGS:
                try:
                    payload = await asyncio.to_thread(
                        _get_json,
                        f"{OKX_REST_BASE}/api/v5/public/liquidation-orders"
                        f"?instType=SWAP&uly={uly}&state=filled",
                    )
                    self.buffer.add(parse_okx_data(payload.get("data") or [], self.contracts, "rest"))
                except Exception as e:
                    logger.warning("OKX REST liquidation poll failed",
                                   extra={"uly": uly, "error": str(e)})
            await asyncio.sleep(REST_POLL_EVERY_S)
