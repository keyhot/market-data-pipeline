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

- **Kraken Futures** (`PF_XBTUSD`, `PF_ETHUSD` by default, `LIQ_KRAKEN_SYMBOLS`
  to override) has no push feed to subscribe to: its public trade-history
  REST endpoint is polled every `LIQ_KRAKEN_POLL_SECONDS` (default 5s) for
  the newest ≤100 trades, `type`-filtered down to `liquidation`/`partial
  liquidation` rows (`process_kraken_response`, pure parsing in
  `ingestion/liquidations.py`). It has no `since`-forward paging, so a busy
  window can outrun the 100-trade cap between polls; `kraken_gap` detects
  and counts that (`self.kraken_gaps`) rather than silently losing rows.
  Read-only and keyless like OKX's REST audit; a failed poll is logged and
  retried next tick, never raised into the shared `asyncio.gather`.

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
from datetime import datetime

import websockets

from ingestion.liquidations import (
    BINANCE,
    BYBIT,
    OKX,
    CoverageTracker,
    KrakenContract,
    KrakenMinuteAgg,
    KrakenTrade,
    Liquidation,
    OkxContract,
    aggregate_kraken_minutes,
    kraken_contracts,
    kraken_coverage_minutes,
    kraken_gap,
    kraken_liquidations,
    kraken_new_trades,
    okx_contracts,
    parse_binance,
    parse_bybit,
    parse_kraken_history,
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
KRAKEN_REST_BASE = "https://futures.kraken.com"
KRAKEN_HISTORY_PATH = "/derivatives/api/v3/history"
KRAKEN_INSTRUMENTS_PATH = "/derivatives/api/v3/instruments"
# A uid seen this many polls ago can never recur: the history endpoint caps
# at 100 rows, so keeping a few windows' worth of uids is plenty to dedupe
# the all-trade minute aggregate across polls without growing unbounded.
KRAKEN_SEEN_UID_CAP = 400
KRAKEN_SYMBOLS_ENV = "LIQ_KRAKEN_SYMBOLS"
KRAKEN_SYMBOLS_DEFAULT = ("PF_XBTUSD", "PF_ETHUSD")
KRAKEN_POLL_SECONDS_ENV = "LIQ_KRAKEN_POLL_SECONDS"
# A real recorded window (tests/fixtures/liquidations/kraken_trade_history_
# pf_xbtusd.json) filled its 100-trade cap in ~6s on an UNEXCEPTIONAL BTC
# perp moment — a cascade, the only window that actually matters here, is
# busier than that, not calmer. 5s is a guess, not a measured margin:
# kraken_gap() exists because this interval will sometimes lose the race,
# and it counts that rather than hiding it.
KRAKEN_POLL_SECONDS_DEFAULT = 5.0

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


def _kraken_mark_seen(seen: dict[str, None], uids) -> None:
    """Record uids as folded into the minute aggregate; evict the oldest."""
    for u in uids:
        seen.pop(u, None)
        seen[u] = None
    overflow = len(seen) - KRAKEN_SEEN_UID_CAP
    if overflow > 0:
        for u in list(seen)[:overflow]:
            del seen[u]


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
        # Separate from `coverage`: per-symbol, gap-aware, and keyed on
        # exchange event time rather than this process's wall-clock liveness
        # proofs — doesn't fit CoverageTracker's per-venue grain, so it gets
        # its own table (storage/liquidation_store.py, liquidation_coverage_kraken).
        self.kraken_coverage: list[tuple[str, int, bool, int]] = []
        self.kraken_minutes: list[KrakenMinuteAgg] = []
        self.rows_written = 0
        self.rows_dropped = 0

    def add(self, rows: list[Liquidation]) -> None:
        self.liquidations.extend(rows)
        self._trim()

    def cover(self, venue: str, minutes: list[tuple[int, int]]) -> None:
        self.coverage.extend((venue, m, n) for m, n in minutes)

    def cover_kraken(self, symbol: str, minutes: list[tuple[int, bool, int]]) -> None:
        self.kraken_coverage.extend(
            (symbol, m, covered, n) for m, covered, n in minutes
        )

    def add_kraken_minutes(self, aggs: list[KrakenMinuteAgg]) -> None:
        self.kraken_minutes.extend(aggs)

    def take(
        self,
    ) -> tuple[
        list[Liquidation],
        list[tuple[str, int, int]],
        list[KrakenMinuteAgg],
        list[tuple[str, int, bool, int]],
    ]:
        batch = (
            self.liquidations,
            self.coverage,
            self.kraken_minutes,
            self.kraken_coverage,
        )
        self.liquidations, self.coverage, self.kraken_minutes, self.kraken_coverage = (
            [],
            [],
            [],
            [],
        )
        return batch

    def restore(
        self,
        liquidations: list[Liquidation],
        coverage: list,
        kraken_minutes: list | None = None,
        kraken_coverage: list | None = None,
    ) -> None:
        self.liquidations = liquidations + self.liquidations
        self.coverage = coverage + self.coverage
        self.kraken_minutes = (kraken_minutes or []) + self.kraken_minutes
        self.kraken_coverage = (kraken_coverage or []) + self.kraken_coverage
        self._trim()

    def _trim(self) -> None:
        overflow = len(self.liquidations) - self._max_rows
        if overflow > 0:
            del self.liquidations[:overflow]
            self.coverage.clear()
            self.kraken_coverage.clear()
            self.rows_dropped += overflow
            logger.error(
                "Liquidation buffer overflowed; "
                "oldest rows and pending coverage dropped",
                extra={"dropped": overflow},
            )


class LiquidationRecorder:
    def __init__(
        self,
        write: Callable,
        binance_url: str | None = None,
        clock: Callable[[], float] = time.time,
        kraken_symbols: tuple[str, ...] | None = None,
        kraken_poll_seconds: float | None = None,
    ):
        self._write = write
        self.buffer = Buffer()
        self._binance_url = (
            binance_url or os.environ.get(BINANCE_URL_ENV) or BINANCE_WS_URL
        )
        self._clock = clock
        self.contracts: dict[str, OkxContract] = {}
        self.trackers = {
            BINANCE: CoverageTracker(),
            OKX: CoverageTracker(),
            BYBIT: CoverageTracker(),
        }
        if kraken_symbols is not None:
            self._kraken_symbols = kraken_symbols
        else:
            raw = os.environ.get(KRAKEN_SYMBOLS_ENV)
            self._kraken_symbols = (
                tuple(s.strip() for s in raw.split(",") if s.strip())
                if raw
                else KRAKEN_SYMBOLS_DEFAULT
            )
        self._kraken_poll_seconds = kraken_poll_seconds or float(
            os.environ.get(KRAKEN_POLL_SECONDS_ENV, KRAKEN_POLL_SECONDS_DEFAULT)
        )
        # Last trade timestamp forwarded per symbol; None means "never
        # polled successfully yet", which also silences gap detection (there
        # is nothing to have a gap relative to).
        self._kraken_last_ts: dict[str, datetime | None] = {
            s: None for s in self._kraken_symbols
        }
        # First minute (epoch s) not yet written to liquidation_coverage_kraken,
        # per symbol — its own cursor, NOT "the minute after last_ts" (KI-104:
        # that skipped the minute last_ts sat in, i.e. every minute that had
        # a trade). None until the first poll returns trades.
        self._kraken_cover_from: dict[str, int | None] = {
            s: None for s in self._kraken_symbols
        }
        self.kraken_gaps: dict[str, int] = {s: 0 for s in self._kraken_symbols}
        self.kraken_contracts: dict[str, KrakenContract] = {}
        # Trade uids already folded into the all-trade minute aggregate, per
        # symbol, oldest-first — bounded so a long-running process doesn't
        # grow this forever (KRAKEN_SEEN_UID_CAP).
        self._kraken_seen_uids: dict[str, dict[str, None]] = {
            s: {} for s in self._kraken_symbols
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

    def process_kraken_response(self, symbol: str, payload: dict) -> list[KrakenTrade]:
        """One poll's worth of a symbol's trade history, synchronous and testable.

        Three independent things happen against this symbol's own
        last-forwarded timestamp (held in memory — a restart starts the gap
        count over, the same way a venue reconnect doesn't blame minutes
        before it on the new connection):

        1. gap detection (unchanged) and, from it, which whole minutes since
           the last poll are provably covered vs. contain a detected gap
           (`kraken_coverage_minutes`);
        2. the all-trade-per-minute aggregate (K2), fed every trade this poll
           hasn't already folded in — deduped by `uid`, NOT by the
           `last_ts` cutoff below, because two distinct trades can share an
           `event_ts` (confirmed in the real fixture) and a ts-cutoff would
           silently drop the later one;
        3. liquidation-row forwarding (unchanged): only rows strictly newer
           than `last_ts`.
        """
        trades = parse_kraken_history(payload, symbol)
        last_ts = self._kraken_last_ts.get(symbol)
        gap = kraken_gap(trades, last_ts)
        if gap:
            self.kraken_gaps[symbol] = self.kraken_gaps.get(symbol, 0) + 1
            logger.warning(
                "Kraken trade-history gap: more than one window's worth of "
                "trades landed between polls",
                extra={
                    "symbol": symbol,
                    "last_ts": last_ts.isoformat() if last_ts else None,
                    "oldest_in_window": min(t.event_ts for t in trades).isoformat(),
                    "gap_count": self.kraken_gaps[symbol],
                },
            )

        minutes, self._kraken_cover_from[symbol] = kraken_coverage_minutes(
            self._kraken_cover_from.get(symbol), trades, gap
        )
        if minutes:
            self.buffer.cover_kraken(symbol, minutes)

        seen = self._kraken_seen_uids.setdefault(symbol, {})
        fresh = [t for t in trades if t.uid not in seen]
        _kraken_mark_seen(seen, (t.uid for t in trades))
        if fresh:
            aggs = aggregate_kraken_minutes(fresh)
            if aggs:
                self.buffer.add_kraken_minutes(aggs)

        new = kraken_new_trades(trades, last_ts)
        if new:
            self._kraken_last_ts[symbol] = max(t.event_ts for t in new)
            liquidations = kraken_liquidations(new, self.kraken_contracts.get(symbol))
            if liquidations:
                self.buffer.add(liquidations)
        return new

    def on_pong(self, venue: str) -> None:
        self.trackers[venue].proof(self._clock())

    def collect_coverage(self) -> None:
        for venue, tracker in self.trackers.items():
            minutes = tracker.confirmed()
            if minutes:
                self.buffer.cover(venue, minutes)

    def write_batch(
        self,
        liquidations: list,
        coverage: list,
        kraken_minutes: list | None = None,
        kraken_coverage: list | None = None,
    ) -> bool:
        """The writer-thread half of a flush: True when the batch landed."""
        kraken_minutes = kraken_minutes or []
        kraken_coverage = kraken_coverage or []
        if (
            not liquidations
            and not coverage
            and not kraken_minutes
            and not kraken_coverage
        ):
            return True
        try:
            self._write(liquidations, coverage, kraken_minutes, kraken_coverage)
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
        liquidations, coverage, kraken_minutes, kraken_coverage = self.buffer.take()
        ok = await asyncio.to_thread(
            self.write_batch, liquidations, coverage, kraken_minutes, kraken_coverage
        )
        if not ok:
            self.buffer.restore(liquidations, coverage, kraken_minutes, kraken_coverage)
        return ok

    # --- network --------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(
            self._venue_loop(BINANCE, self._binance_session),
            self._venue_loop(OKX, self._okx_session),
            self._venue_loop(BYBIT, self._bybit_session),
            self._flush_loop(),
            self._okx_rest_loop(),
            self._kraken_rest_loop(),
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
            logger.info(
                "Binance liquidation stream connected", extra={"url": self._binance_url}
            )
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
                        _get_json,
                        f"{OKX_REST_BASE}/api/v5/public/instruments?instType=SWAP",
                    )
                    fresh = okx_contracts(payload)
                    if fresh:
                        self.contracts = fresh
                        contracts_at = self._clock()
                except Exception as e:
                    logger.warning(
                        "OKX instruments fetch failed", extra={"error": str(e)}
                    )
            for uly in OKX_REST_UNDERLYINGS:
                try:
                    payload = await asyncio.to_thread(
                        _get_json,
                        f"{OKX_REST_BASE}/api/v5/public/liquidation-orders"
                        f"?instType=SWAP&uly={uly}&state=filled",
                    )
                    self.buffer.add(
                        parse_okx_data(
                            payload.get("data") or [], self.contracts, "rest"
                        )
                    )
                except Exception as e:
                    logger.warning(
                        "OKX REST liquidation poll failed",
                        extra={"uly": uly, "error": str(e)},
                    )
            await asyncio.sleep(REST_POLL_EVERY_S)

    async def _kraken_rest_loop(self) -> None:
        # Every exception is caught inside the loop body: this task shares
        # an asyncio.gather with Binance/OKX/Bybit in run(), and an
        # unhandled exception here would take the whole process — including
        # the other venues' live sockets — down with it.
        backoff = self._kraken_poll_seconds
        contracts_at = 0.0
        while True:
            if self._clock() - contracts_at > CONTRACTS_REFRESH_S:
                try:
                    payload = await asyncio.to_thread(
                        _get_json, f"{KRAKEN_REST_BASE}{KRAKEN_INSTRUMENTS_PATH}"
                    )
                    fresh = kraken_contracts(payload)
                    if fresh:
                        self.kraken_contracts = fresh
                        contracts_at = self._clock()
                except Exception as e:
                    logger.warning(
                        "Kraken instruments fetch failed", extra={"error": str(e)}
                    )
            ok = True
            for symbol in self._kraken_symbols:
                try:
                    payload = await asyncio.to_thread(
                        _get_json,
                        f"{KRAKEN_REST_BASE}{KRAKEN_HISTORY_PATH}?symbol={symbol}",
                    )
                    self.process_kraken_response(symbol, payload)
                except Exception as e:
                    ok = False
                    logger.warning(
                        "Kraken trade-history poll failed",
                        extra={"symbol": symbol, "error": f"{type(e).__name__}: {e}"},
                    )
            backoff = (
                self._kraken_poll_seconds if ok else min(backoff * 2, _BACKOFF_MAX_S)
            )
            await asyncio.sleep(backoff)
