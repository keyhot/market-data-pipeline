"""The recorder's async loops, driven on virtual time (Sprint 23 A7, stage 1).

No production seam is needed: the clock is injected (`clock=`), `_venue_loop`
takes its `session`, the keepalives take their `ws`, and `asyncio`,
`websockets.connect` and `_get_json` are module-level names. So:

- `asyncio` as seen by `ingestion.liquidation_recorder` is swapped for a proxy
  whose `sleep` records the requested delay, advances the injected clock by
  it (or by a per-test step) and yields once; every other attribute is the real
  asyncio, so the event loop, `to_thread` and `CancelledError` are untouched.
  The Nth sleep raises `_Stop` (a BaseException, so no `except Exception` in a
  loop can swallow it) and the delay it was asked for is still recorded.
- `websockets.connect` is a fake socket; `_get_json` routes by URL and fails
  the test on any URL it does not expect.
- Nothing here waits in real time and no socket is opened.
"""

import asyncio as real_asyncio
import json
import socket

import pytest

import ingestion.liquidation_recorder as lr
from ingestion.liquidation_recorder import (
    FLUSH_EVERY_S,
    PING_EVERY_S,
    PONG_TIMEOUT_S,
    REST_POLL_EVERY_S,
    STALE_AFTER_S,
    LiquidationRecorder,
)
from ingestion.liquidations import (
    BINANCE,
    BYBIT,
    OKX,
    parse_binance,
    parse_kraken_history,
)
from tests.unit.test_liquidation_recorder import (
    BYBIT_PONG,
    BYBIT_SUBSCRIBED,
    OKX_SUBSCRIBED,
    T0,
    Clock,
    Sink,
)
from tests.unit.test_liquidations import (
    BINANCE_FRAME,
    KRAKEN_INSTRUMENTS_FIXTURE,
    KRAKEN_PAYLOAD,
    OKX_FRAME,
)

HOUR = 3600.0
KRAKEN_INSTRUMENTS = json.loads(KRAKEN_INSTRUMENTS_FIXTURE.read_text())
OKX_INSTRUMENTS_V1 = {
    "data": [
        {"instId": "NMR-USDT-SWAP", "ctVal": "0.1", "ctMult": "1", "ctType": "linear"}
    ]
}
OKX_INSTRUMENTS_V2 = {
    "data": [
        {"instId": "NMR-USDT-SWAP", "ctVal": "0.2", "ctMult": "1", "ctType": "linear"}
    ]
}


@pytest.fixture(autouse=True)
def no_socket_connects(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError("the loop tests open no network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# --- virtual time ------------------------------------------------------------


class _Stop(BaseException):
    """Ends a `while True` loop under test from inside its own sleep."""


class Sleeper:
    def __init__(self, clock, stop_after=None, step=None, on_sleep=None):
        self.clock = clock
        self.stop_after = stop_after
        self.step = step
        self.on_sleep = on_sleep
        self.delays = []
        self.timeouts = []

    async def sleep(self, delay):
        self.delays.append(delay)
        if self.stop_after is not None and len(self.delays) >= self.stop_after:
            raise _Stop
        self.clock.t += delay if self.step is None else self.step
        if self.on_sleep is not None:
            self.on_sleep(len(self.delays))
        await real_asyncio.sleep(0)

    async def wait_for(self, fut, timeout):
        # Records the timeout; an unresolved pong waiter is a timeout at once.
        self.timeouts.append(timeout)
        if not fut.done():
            raise real_asyncio.TimeoutError
        return await fut


class _AsyncioProxy:
    def __init__(self, sleeper):
        self.sleep = sleeper.sleep
        self.wait_for = sleeper.wait_for

    def __getattr__(self, name):
        return getattr(real_asyncio, name)


def _install(monkeypatch, sleeper):
    monkeypatch.setattr(lr, "asyncio", _AsyncioProxy(sleeper))
    return sleeper


def _run_bounded(coro, seconds=2.0):
    """For sessions that end only when the keepalive closes the socket: if a
    regression stops it closing, fail after `seconds` instead of hanging."""
    return real_asyncio.run(real_asyncio.wait_for(coro, seconds))


def _drive(coro):
    """Run a loop coroutine until its sleeper stops it."""
    with pytest.raises(_Stop):
        real_asyncio.run(coro)


def _setup(monkeypatch, *, stop_after=None, step=None, on_sleep=None, sink=None, **kw):
    clock = Clock(T0)
    sleeper = _install(monkeypatch, Sleeper(clock, stop_after, step, on_sleep))
    recorder = LiquidationRecorder(sink or Sink(), clock=clock, **kw)
    return recorder, clock, sleeper


# --- fake websocket ----------------------------------------------------------


class FakeWS:
    """Frames, then silence until `close()` or `finish()` (like a live socket)."""

    def __init__(self, frames=(), ping=None, on_send=None):
        self.frames = list(frames)
        self.sent = []
        self.closed = 0
        self.finished = False
        self._ping = ping
        self._on_send = on_send
        self._wake = real_asyncio.Event()

    async def send(self, message):
        self.sent.append(message)
        if self._on_send is not None:
            self._on_send(self, message)

    def push(self, frame):
        self.frames.append(frame)
        self._wake.set()

    def finish(self):
        """The test (not the keepalive) ends the connection."""
        self.finished = True
        self._wake.set()

    async def close(self):
        self.closed += 1
        self._wake.set()

    async def ping(self):
        if self._ping is not None:
            return self._ping()
        fut = real_asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if self.frames:
                return self.frames.pop(0)
            if self.closed or self.finished:
                raise StopAsyncIteration
            self._wake.clear()
            await self._wake.wait()


def _connect_to(monkeypatch, ws):
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return ws

    monkeypatch.setattr(lr.websockets, "connect", connect)
    return calls


# --- _venue_loop: backoff and reconnect ---------------------------------------


def _venue(monkeypatch, lifetimes, stop_after, outcome="fail", venue=OKX):
    """Run `_venue_loop` with sessions that live `lifetimes` seconds each, then 0."""
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=stop_after)
    it = iter(lifetimes)
    sessions = []

    async def session():
        sessions.append(clock.t)
        recorder.trackers[venue].proof(clock.t)
        clock.t += next(it, 0)
        if outcome == "fail":
            raise ConnectionError("dropped")

    _drive(recorder._venue_loop(venue, session))
    return recorder, sleeper, sessions


def test_backoff_doubles_to_the_cap_while_sessions_die_at_once(monkeypatch):
    _, sleeper, sessions = _venue(monkeypatch, [], stop_after=9)
    assert sleeper.delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert len(sessions) == 9  # every sleep is followed by a reconnect attempt


def test_backoff_resets_after_a_session_that_lived_a_while(monkeypatch):
    _, sleeper, _ = _venue(monkeypatch, [0, 0, 61], stop_after=5)
    assert sleeper.delays == [1, 2, 1, 2, 4]


def test_backoff_is_not_reset_by_a_session_of_exactly_sixty_seconds(monkeypatch):
    _, sleeper, _ = _venue(monkeypatch, [0, 60], stop_after=3)
    assert sleeper.delays == [1, 2, 4]


def test_backoff_resets_for_a_session_of_sixty_one_seconds(monkeypatch):
    _, sleeper, _ = _venue(monkeypatch, [0, 61], stop_after=3)
    assert sleeper.delays == [1, 1, 2]


def test_a_long_session_resets_a_backoff_that_had_reached_the_cap(monkeypatch):
    # Seven quick deaths (1..60), a long life, then a quick death: back to 1.
    _, sleeper, _ = _venue(monkeypatch, [0] * 7 + [3600], stop_after=9)
    assert sleeper.delays == [1, 2, 4, 8, 16, 32, 60, 1, 2]


def test_a_clean_close_keeps_the_backoff_doubling(monkeypatch):
    _, sleeper, _ = _venue(monkeypatch, [], stop_after=3, outcome="clean")
    assert sleeper.delays == [1, 2, 4]


def test_the_venue_tracker_is_reset_when_its_session_ends(monkeypatch):
    # Proofs from a dead connection must not bridge into the next one.
    for outcome in ("fail", "clean"):
        recorder, _, _ = _venue(
            monkeypatch, [], stop_after=2, outcome=outcome, venue=BYBIT
        )
        assert recorder.trackers[BYBIT].last_proof is None


def test_cancelling_a_session_ends_the_loop_without_a_backoff_sleep(monkeypatch):
    recorder, clock, sleeper = _setup(monkeypatch)

    async def session():
        recorder.trackers[OKX].proof(clock.t)
        raise real_asyncio.CancelledError

    with pytest.raises(real_asyncio.CancelledError):
        real_asyncio.run(recorder._venue_loop(OKX, session))
    assert sleeper.delays == []
    assert recorder.trackers[OKX].last_proof is None


# --- _app_keepalive (OKX, Bybit) ---------------------------------------------


def test_app_keepalive_pings_every_tick_then_closes_a_silent_socket(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch, stop_after=20)
    ws = FakeWS()
    real_asyncio.run(recorder._app_keepalive(OKX, ws, "ping"))
    # No proof ever: 10s, 20s and 30s of silence still ping (the limit is a
    # strict `>` STALE_AFTER_S); the 40s tick closes and the loop returns.
    assert STALE_AFTER_S == 3 * PING_EVERY_S
    assert ws.sent == ["ping"] * 3
    assert ws.closed == 1
    assert sleeper.delays == [PING_EVERY_S] * 4


def test_app_keepalive_stays_up_while_replies_keep_arriving(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch, stop_after=9)
    ws = FakeWS(on_send=lambda ws, m: recorder.on_okx_frame("pong"))
    _drive(recorder._app_keepalive(OKX, ws, "ping"))
    assert ws.sent == ["ping"] * 8
    assert ws.closed == 0


def test_app_keepalive_closes_once_replies_stop(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch, stop_after=20)

    def reply_twice(ws, message):
        if len(ws.sent) <= 2:
            recorder.on_okx_frame("pong")

    ws = FakeWS(on_send=reply_twice)
    real_asyncio.run(recorder._app_keepalive(OKX, ws, "ping"))
    # Last proof at +20s; pings at +30..+50s (<= 30s of silence); +60s is 40s.
    assert ws.sent == ["ping"] * 5
    assert ws.closed == 1
    assert sleeper.delays == [PING_EVERY_S] * 6


def test_app_keepalive_measures_silence_from_the_last_proof_not_the_connect(
    monkeypatch,
):
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=20)
    # One reply at tick 3 (+30s); without it the socket would close at +40s.
    ws = FakeWS(
        on_send=lambda ws, m: (
            recorder.on_okx_frame("pong") if len(ws.sent) == 3 else None
        )
    )
    real_asyncio.run(recorder._app_keepalive(OKX, ws, "ping"))
    assert sleeper.delays == [PING_EVERY_S] * 7  # closes at +70s
    assert ws.sent == ["ping"] * 6
    assert ws.closed == 1


@pytest.mark.parametrize(
    "session, venue, url_const, subscribe_const, ping, subscribed",
    [
        ("_okx_session", OKX, "OKX_WS_URL", "OKX_SUBSCRIBE", "ping", OKX_SUBSCRIBED),
        (
            "_bybit_session",
            BYBIT,
            "BYBIT_WS_URL",
            "BYBIT_SUBSCRIBE",
            lr.BYBIT_PING,
            BYBIT_SUBSCRIBED,
        ),
    ],
)
def test_acked_session_subscribes_pings_and_ends_when_the_keepalive_closes_it(
    monkeypatch, session, venue, url_const, subscribe_const, ping, subscribed
):
    recorder, _, sleeper = _setup(monkeypatch)
    ws = FakeWS(frames=[subscribed])
    connects = _connect_to(monkeypatch, ws)
    _run_bounded(getattr(recorder, session)())
    assert connects == [(getattr(lr, url_const), {"ping_interval": None})]
    # The ack is the only proof, at connect time; three pings, then the close.
    assert recorder.trackers[venue].last_proof == T0  # the ack was counted as a proof
    assert ws.sent == [getattr(lr, subscribe_const), ping, ping, ping]
    assert ws.closed == 1
    assert sleeper.delays == [PING_EVERY_S] * 4


def test_acked_session_pong_replies_keep_the_socket_open(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch)

    def answer(ws, message):
        if message == lr.BYBIT_PING:
            ws.push(BYBIT_PONG)
            if ws.sent.count(lr.BYBIT_PING) == 6:
                ws.finish()  # the test ends the session, not the keepalive

    ws = FakeWS(frames=[BYBIT_SUBSCRIBED], on_send=answer)
    _connect_to(monkeypatch, ws)
    _run_bounded(recorder._bybit_session())
    assert ws.sent.count(lr.BYBIT_PING) == 6
    assert ws.closed == 0
    assert sleeper.delays[:6] == [PING_EVERY_S] * 6


def test_acked_session_error_frame_raises_so_the_venue_loop_reconnects(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch)
    ws = FakeWS(frames=['{"event":"error","code":"60012","msg":"bad request"}'])
    _connect_to(monkeypatch, ws)
    with pytest.raises(RuntimeError, match="okx refused a request"):
        real_asyncio.run(recorder._okx_session())
    assert recorder.trackers[OKX].last_proof is None  # an error is no proof
    assert ws.sent == [lr.OKX_SUBSCRIBE]  # the keepalive was cancelled before it pinged


# --- _binance_keepalive --------------------------------------------------------


def test_binance_keepalive_turns_every_answered_ping_into_a_proof(monkeypatch):
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=4)
    ws = FakeWS()
    _drive(recorder._binance_keepalive(ws))
    assert sleeper.delays == [PING_EVERY_S] * 4
    assert sleeper.timeouts == [PONG_TIMEOUT_S] * 3
    assert recorder.trackers[BINANCE].last_proof == T0 + 3 * PING_EVERY_S
    assert ws.closed == 0


def test_binance_keepalive_carries_coverage_through_a_quiet_feed(monkeypatch):
    # One liquidation at T0, then silence: only the pongs prove the later minutes.
    recorder, clock, _ = _setup(monkeypatch, stop_after=13)
    recorder.on_binance_frame(BINANCE_FRAME)
    _drive(recorder._binance_keepalive(FakeWS()))
    recorder.collect_coverage()
    n_rows = len(parse_binance(BINANCE_FRAME))
    assert recorder.buffer.coverage == [(BINANCE, T0, n_rows), (BINANCE, T0 + 60, 0)]


def test_binance_keepalive_closes_when_the_pong_never_comes(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch, stop_after=20)

    def never():
        return real_asyncio.get_running_loop().create_future()

    ws = FakeWS(ping=never)
    real_asyncio.run(recorder._binance_keepalive(ws))
    assert ws.closed == 1
    assert sleeper.delays == [PING_EVERY_S]
    assert recorder.trackers[BINANCE].last_proof is None


def test_binance_keepalive_closes_when_the_ping_itself_fails(monkeypatch):
    recorder, _, sleeper = _setup(monkeypatch, stop_after=20)

    def boom():
        raise ConnectionError("socket gone")

    ws = FakeWS(ping=boom)
    real_asyncio.run(recorder._binance_keepalive(ws))
    assert ws.closed == 1
    assert recorder.trackers[BINANCE].last_proof is None


def test_binance_keepalive_closes_after_the_good_pongs_when_a_later_one_is_missed(
    monkeypatch,
):
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=20)
    n = {"pings": 0}

    def sometimes():
        n["pings"] += 1
        fut = real_asyncio.get_running_loop().create_future()
        if n["pings"] <= 2:
            fut.set_result(None)
        return fut

    ws = FakeWS(ping=sometimes)
    real_asyncio.run(recorder._binance_keepalive(ws))
    assert ws.closed == 1
    assert sleeper.delays == [PING_EVERY_S] * 3
    assert recorder.trackers[BINANCE].last_proof == T0 + 2 * PING_EVERY_S


# --- _flush_loop -----------------------------------------------------------------


def test_flush_loop_sleeps_first_then_writes_what_arrived(monkeypatch):
    sink = Sink()
    seen_at_sleep = []
    recorder_box = {}

    def on_sleep(n):
        seen_at_sleep.append(len(sink.batches))
        if n == 2:  # a row arrives just before the second flush
            recorder_box["r"].on_binance_frame(BINANCE_FRAME)

    recorder, _, sleeper = _setup(
        monkeypatch, stop_after=4, on_sleep=on_sleep, sink=sink
    )
    recorder_box["r"] = recorder
    recorder.on_binance_frame(BINANCE_FRAME)
    n_rows = len(parse_binance(BINANCE_FRAME))
    _drive(recorder._flush_loop())
    assert sleeper.delays == [FLUSH_EVERY_S] * 4
    assert seen_at_sleep == [0, 1, 2]  # nothing is written before the first sleep
    assert [len(b[0]) for b in sink.batches] == [n_rows, n_rows]
    assert recorder.buffer.rows_written == 2 * n_rows
    assert recorder.buffer.liquidations == []


def test_flush_loop_retries_a_failed_write_with_the_same_rows_next_tick(monkeypatch):
    sink = Sink(fail=True)

    def on_sleep(n):
        if n == 2:
            sink.fail = False  # postgres is back before the second flush

    recorder, _, sleeper = _setup(
        monkeypatch, stop_after=4, on_sleep=on_sleep, sink=sink
    )
    recorder.on_binance_frame(BINANCE_FRAME)
    n_rows = len(parse_binance(BINANCE_FRAME))
    _drive(recorder._flush_loop())
    assert len(sink.batches) == 1
    assert len(sink.batches[0][0]) == n_rows
    assert recorder.buffer.rows_written == n_rows


class SeenSink(Sink):
    """A Sink that also takes the fold-state keyword, and logs every attempt."""

    def __init__(self, fail=False):
        super().__init__(fail=fail)
        self.attempts = []  # (kraken_seen, batch contents) of every call, failed or not
        self.seen = []  # kraken_seen of the calls that landed

    def __call__(
        self,
        liquidations,
        coverage,
        kraken_minutes=(),
        kraken_coverage=(),
        kraken_seen=None,
    ):
        self.attempts.append(
            (liquidations, coverage, kraken_minutes, kraken_coverage, kraken_seen)
        )
        super().__call__(liquidations, coverage, kraken_minutes, kraken_coverage)
        self.seen.append(kraken_seen)


def test_flush_loop_failed_write_keeps_coverage_and_kraken_state_for_the_retry(
    monkeypatch,
):
    sink = SeenSink(fail=True)

    def on_sleep(n):
        if n == 2:
            sink.fail = False  # postgres is back before the second flush

    recorder, clock, _ = _setup(
        monkeypatch,
        stop_after=4,
        on_sleep=on_sleep,
        sink=sink,
        kraken_symbols=("PF_XBTUSD",),
    )
    recorder.on_binance_frame(BINANCE_FRAME)  # data at T0 (a whole minute)
    for i in range(1, 13):  # a pong every 10s for two minutes
        clock.t = T0 + 10 * i
        recorder.on_pong(BINANCE)
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    uids = [t.uid for t in parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")]
    n_rows = len(parse_binance(BINANCE_FRAME))
    assert uids and recorder.buffer.kraken_minutes

    _drive(recorder._flush_loop())

    # the first, failed attempt carried the coverage and the Kraken state ...
    first, _retry = sink.attempts
    assert first[1] == [(BINANCE, T0, n_rows), (BINANCE, T0 + 60, 0)]
    assert first[4] == {"PF_XBTUSD": uids}
    # ... and the retry that landed carries all of it again, nothing lost
    (batch,) = sink.batches
    assert len(batch[0]) == n_rows
    assert batch[1] == first[1]
    assert batch[2] == first[2] and batch[2]
    assert sink.seen == [{"PF_XBTUSD": uids}]
    # and a landed batch leaves nothing behind to be written a third time
    assert recorder.buffer.kraken_seen == {}
    assert recorder.buffer.coverage == []


def test_flush_loop_writes_the_coverage_minutes_it_collected(monkeypatch):
    sink = Sink()
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=2, sink=sink)
    recorder.on_binance_frame(BINANCE_FRAME)  # data at T0 (a whole minute)
    for i in range(1, 13):  # a pong every 10s for two minutes
        clock.t = T0 + 10 * i
        recorder.on_pong(BINANCE)
    n_rows = len(parse_binance(BINANCE_FRAME))
    _drive(recorder._flush_loop())
    (batch,) = sink.batches
    assert batch[1] == [(BINANCE, T0, n_rows), (BINANCE, T0 + 60, 0)]


# --- _okx_rest_loop: contracts refresh ---------------------------------------------


class Http:
    """`_get_json` stand-in: routes by URL, fails on any URL it was not given."""

    def __init__(self, instruments=(), history=None, liquidation_orders=None):
        self.instruments = list(instruments)
        self.history = history
        self.liquidation_orders = (
            liquidation_orders if liquidation_orders is not None else {"data": []}
        )
        self.urls = []

    def __call__(self, url, timeout=10.0):
        self.urls.append(url)
        if url.endswith("/instruments?instType=SWAP") or url.endswith("/instruments"):
            item = (
                self.instruments.pop(0)
                if len(self.instruments) > 1
                else self.instruments[0]
            )
            if isinstance(item, Exception):
                raise item
            return item
        if "/api/v5/public/liquidation-orders" in url:
            return self.liquidation_orders
        if "/derivatives/api/v3/history" in url:
            if isinstance(self.history, Exception):
                raise self.history
            return self.history if self.history is not None else {"history": []}
        raise AssertionError(f"unexpected URL {url}")

    def count(self, needle):
        return sum(needle in u for u in self.urls)


def _http(monkeypatch, **kw):
    http = Http(**kw)
    monkeypatch.setattr(lr, "_get_json", http)
    return http


def test_okx_instruments_are_refetched_after_six_hours_and_not_before(monkeypatch):
    http = _http(monkeypatch, instruments=[OKX_INSTRUMENTS_V1, OKX_INSTRUMENTS_V2])
    per_pass = []
    recorder, clock, sleeper = _setup(
        monkeypatch,
        stop_after=4,
        step=4 * HOUR,
        on_sleep=lambda n: per_pass.append(http.count("/instruments")),
    )
    _drive(recorder._okx_rest_loop())
    # Passes at +0h (first fetch), +4h (not due), +8h (due: > 6h since the fetch).
    assert per_pass == [1, 1, 2]
    assert recorder.contracts["NMR-USDT-SWAP"].ct_val == 0.2
    assert sleeper.delays == [REST_POLL_EVERY_S] * 4


def test_okx_instruments_refresh_boundary_is_strictly_greater_than_six_hours(
    monkeypatch,
):
    http = _http(monkeypatch, instruments=[OKX_INSTRUMENTS_V1])
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=2, step=6 * HOUR)
    _drive(recorder._okx_rest_loop())
    assert http.count("/instruments") == 1  # exactly 6h after the fetch: not due yet
    http2 = _http(monkeypatch, instruments=[OKX_INSTRUMENTS_V1])
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=2, step=6 * HOUR + 1)
    _drive(recorder._okx_rest_loop())
    assert http2.count("/instruments") == 2


def test_okx_refresh_keeps_old_contracts_when_a_refetch_is_empty_or_fails_and_retries(
    monkeypatch,
):
    http = _http(
        monkeypatch,
        instruments=[
            OKX_INSTRUMENTS_V1,
            {"data": []},
            ConnectionError("down"),
            OKX_INSTRUMENTS_V2,
        ],
    )
    after_pass = []
    holder = {}
    recorder, clock, sleeper = _setup(
        monkeypatch,
        stop_after=5,
        step=7 * HOUR,  # every pass is due
        on_sleep=lambda n: after_pass.append(
            holder["r"].contracts["NMR-USDT-SWAP"].ct_val
        ),
    )
    holder["r"] = recorder
    _drive(recorder._okx_rest_loop())
    assert after_pass == [0.1, 0.1, 0.1, 0.2]
    assert http.count("/instruments") == 5  # due on every pass


def test_okx_refresh_that_returned_nothing_is_retried_next_tick_not_in_six_hours(
    monkeypatch,
):
    http = _http(
        monkeypatch,
        instruments=[ConnectionError("down"), {"data": []}, OKX_INSTRUMENTS_V1],
    )
    recorder, clock, sleeper = _setup(monkeypatch, stop_after=6)  # 300s per pass
    _drive(recorder._okx_rest_loop())
    # fail, empty, ok; then 5 minutes later it is not due any more.
    assert http.count("/instruments") == 3
    assert recorder.contracts


def test_okx_rest_poll_buffers_rows_for_both_underlyings_with_the_refreshed_contracts(
    monkeypatch,
):
    payload = {"data": json.loads(OKX_FRAME)["data"]}
    http = _http(
        monkeypatch, instruments=[OKX_INSTRUMENTS_V1], liquidation_orders=payload
    )
    recorder, _, _ = _setup(monkeypatch, stop_after=1)  # one pass
    _drive(recorder._okx_rest_loop())
    rows = recorder.buffer.liquidations
    assert http.count("liquidation-orders") == 2 and http.count("uly=BTC-USDT") == 1
    assert rows and {r.source for r in rows} == {"rest"}
    assert all(r.qty_base is not None for r in rows)  # contracts were loaded first


def test_okx_rest_loop_survives_a_failed_poll_for_one_underlying(monkeypatch):
    class OneBad(Http):
        def __call__(self, url, timeout=10.0):
            if "uly=BTC-USDT" in url:
                self.urls.append(url)
                raise ConnectionError("boom")
            return super().__call__(url, timeout)

    http = OneBad(
        instruments=[OKX_INSTRUMENTS_V1],
        liquidation_orders={"data": json.loads(OKX_FRAME)["data"]},
    )
    monkeypatch.setattr(lr, "_get_json", http)
    recorder, _, _ = _setup(monkeypatch, stop_after=2)  # two passes
    _drive(recorder._okx_rest_loop())
    assert http.count("uly=BTC-USDT") == 2 and http.count("uly=ETH-USDT") == 2
    assert recorder.buffer.liquidations  # ETH's rows still landed


# --- _kraken_rest_loop: contracts refresh, backoff, restart-dedupe wait ---------------


def _kraken(monkeypatch, http, **setup_kw):
    kw = dict(kraken_symbols=("PF_XBTUSD",), kraken_poll_seconds=5.0)
    kw.update(setup_kw.pop("recorder_kw", {}))
    return _setup(monkeypatch, **setup_kw, **kw)


def test_kraken_instruments_are_refetched_after_six_hours_and_not_before(monkeypatch):
    http = _http(monkeypatch, instruments=[KRAKEN_INSTRUMENTS, {"instruments": []}])
    per_pass = []
    recorder, clock, sleeper = _kraken(
        monkeypatch,
        http,
        stop_after=4,
        step=4 * HOUR,
        on_sleep=lambda n: per_pass.append(http.count("/instruments")),
    )
    _drive(recorder._kraken_rest_loop())
    assert per_pass == [1, 1, 2]  # +0h fetch, +4h not due, +8h due
    assert recorder.kraken_contracts  # the empty second answer did not wipe them


def test_kraken_empty_or_failed_refresh_keeps_old_contracts_and_retries_next_tick(
    monkeypatch,
):
    http = _http(
        monkeypatch,
        instruments=[ConnectionError("down"), {"instruments": []}, KRAKEN_INSTRUMENTS],
    )
    recorder, clock, sleeper = _kraken(monkeypatch, http, stop_after=6)  # 5s per pass
    _drive(recorder._kraken_rest_loop())
    # fail, empty, ok, then not due again (5s later, far from 6h).
    assert http.count("/instruments") == 3
    assert recorder.kraken_contracts


def test_kraken_keeps_the_old_contracts_when_a_due_refresh_comes_back_empty(
    monkeypatch,
):
    http = _http(monkeypatch, instruments=[KRAKEN_INSTRUMENTS, {"instruments": []}])
    sizes = []
    holder = {}
    recorder, clock, sleeper = _kraken(
        monkeypatch,
        http,
        stop_after=3,
        step=7 * HOUR,
        on_sleep=lambda n: sizes.append(len(holder["r"].kraken_contracts)),
    )
    holder["r"] = recorder
    _drive(recorder._kraken_rest_loop())
    assert http.count("/instruments") == 3  # due on every pass
    assert sizes[0] > 0 and sizes == [sizes[0], sizes[0]]


def test_kraken_poll_backs_off_while_polls_fail_and_resets_after_a_good_one(
    monkeypatch,
):
    class Flaky(Http):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.history_calls = 0

        def __call__(self, url, timeout=10.0):
            if "/history" in url:
                self.urls.append(url)
                self.history_calls += 1
                if self.history_calls <= 4:
                    raise ConnectionError("history down")
                return {"history": []}
            return super().__call__(url, timeout)

    http = Flaky(instruments=[KRAKEN_INSTRUMENTS])
    monkeypatch.setattr(lr, "_get_json", http)
    recorder, _, sleeper = _kraken(monkeypatch, http, stop_after=8)
    _drive(recorder._kraken_rest_loop())
    # Four failures: 2x, 4x, 8x, then capped at 60; a good poll is back to 5.
    assert sleeper.delays == [10, 20, 40, 60, 5, 5, 5, 5]


def test_kraken_a_poll_failing_for_one_symbol_backs_off_the_whole_loop(monkeypatch):
    class OneSymbolBad(Http):
        def __call__(self, url, timeout=10.0):
            if "symbol=PF_ETHUSD" in url:
                self.urls.append(url)
                raise ConnectionError("eth down")
            return super().__call__(url, timeout)

    http = OneSymbolBad(instruments=[KRAKEN_INSTRUMENTS])
    monkeypatch.setattr(lr, "_get_json", http)
    recorder, _, sleeper = _setup(
        monkeypatch,
        stop_after=3,
        kraken_symbols=("PF_XBTUSD", "PF_ETHUSD"),
        kraken_poll_seconds=5.0,
    )
    _drive(recorder._kraken_rest_loop())
    assert sleeper.delays == [10, 20, 40]
    assert http.count("symbol=PF_XBTUSD") == 3  # the healthy symbol is still polled


def test_kraken_waits_for_the_restart_state_before_polling_or_fetching_anything(
    monkeypatch,
):
    http = _http(monkeypatch, instruments=[KRAKEN_INSTRUMENTS])
    attempts = {"n": 0}
    urls_at_sleep = []

    def loader():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("postgres down")
        return {}

    recorder, _, sleeper = _kraken(
        monkeypatch,
        http,
        stop_after=3,
        on_sleep=lambda n: urls_at_sleep.append(len(http.urls)),
        recorder_kw={"load_kraken_seen": loader},
    )
    _drive(recorder._kraken_rest_loop())
    assert sleeper.delays == [
        10,
        20,
        5,
    ]  # two failed loads back off, then a normal poll
    assert urls_at_sleep == [0, 0]  # no request at all before the state is loaded
    assert recorder._kraken_seeded is True


def test_kraken_restart_does_not_fold_again_the_trades_the_database_already_holds(
    monkeypatch,
):
    uids = [t.uid for t in parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")]
    assert uids

    def folded(loader):
        http = _http(
            monkeypatch, instruments=[KRAKEN_INSTRUMENTS], history=KRAKEN_PAYLOAD
        )
        recorder, _, _ = _kraken(
            monkeypatch, http, stop_after=2, recorder_kw={"load_kraken_seen": loader}
        )
        _drive(recorder._kraken_rest_loop())
        return recorder.buffer.kraken_minutes

    assert folded(lambda: {}) != []  # control: a fresh database folds the window
    assert folded(lambda: {"PF_XBTUSD": uids}) == []  # a restart folds nothing twice


# --- run() -------------------------------------------------------------------


def test_run_starts_every_loop_together(monkeypatch):
    recorder, _, _ = _setup(monkeypatch)
    started = []

    def stub(name):
        async def loop(*args):
            started.append((name, args))

        return loop

    for name in ("_flush_loop", "_okx_rest_loop", "_kraken_rest_loop"):
        monkeypatch.setattr(recorder, name, stub(name))
    monkeypatch.setattr(recorder, "_venue_loop", stub("_venue_loop"))
    real_asyncio.run(recorder.run())
    names = [n for n, _ in started]
    assert sorted(names) == sorted(
        ["_venue_loop"] * 3 + ["_flush_loop", "_okx_rest_loop", "_kraken_rest_loop"]
    )
    assert {args[0] for n, args in started if n == "_venue_loop"} == {
        BINANCE,
        OKX,
        BYBIT,
    }
    assert {args[1].__name__ for n, args in started if n == "_venue_loop"} == {
        "_binance_session",
        "_okx_session",
        "_bybit_session",
    }
