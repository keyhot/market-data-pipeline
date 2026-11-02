"""Low-severity pins from audit 5 (liquidation boundaries): F3-F7 and F9.

Each section below names its finding, the mutant it kills, and why the
existing tests could not. These are new tests in a new file on purpose: the
two existing liquidation test files are rewrapped on `ci-green-lint`, so
editing them in place would conflict.

No database, no sockets: the recorder gets a fake clock and a list-collecting
writer, and everything else is pure.
"""

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from ingestion.liquidation_recorder import (
    KRAKEN_SEEN_UID_CAP,
    LiquidationRecorder,
)
from ingestion.liquidations import CoverageTracker, parse_okx, parse_okx_data
from tests.unit.test_liquidation_recorder import (
    BYBIT_SUBSCRIBED,
    OKX_SUBSCRIBED,
    Sink,
)
from tests.unit.test_liquidations import BINANCE_FRAME, LINEAR, OKX_FRAME

UTC = timezone.utc
T0 = 1_790_650_800  # a whole minute, as in the suite

# --- F3: the OKX event_ts is never asserted -------------------------------------
#
# `parse_okx_data` reads `ts = _ms(detail["ts"])` (ingestion/liquidations.py:195).
# Binance and Bybit assert their `event_ts`; OKX's tests compared REST to WS
# (both through the same line) or looked at the other fields, so a naive
# `datetime.fromtimestamp(int(ts) / 1000)`, a +-60 000 ms shift, or the wrong
# field all stayed green. The expected values below are literals, not `_ms`.

OKX_FRAME_TS = '"ts":"1790650761954"'


def test_okx_ws_event_ts_is_the_detail_ts_as_aware_utc():
    assert OKX_FRAME_TS in OKX_FRAME  # the replacement below can find it
    _, (liq,) = parse_okx(OKX_FRAME, LINEAR)
    assert liq.event_ts == datetime(2026, 9, 29, 2, 59, 21, 954000, tzinfo=UTC)
    # An aware datetime equals the same instant in any zone, so pin the zone:
    # a naive stamp would be read by Postgres in the session TimeZone.
    assert liq.event_ts.utcoffset() == timedelta(0)


def test_okx_rest_event_ts_is_the_detail_ts_as_aware_utc():
    data = json.loads(OKX_FRAME)["data"]
    (liq,) = parse_okx_data(data, LINEAR, "rest")
    assert liq.source == "rest"
    assert liq.event_ts == datetime(2026, 9, 29, 2, 59, 21, 954000, tzinfo=UTC)
    assert liq.event_ts.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "ts_ms,expected",
    [
        # Milliseconds survive (not truncated to the second, not read as seconds).
        ("1790650799999", datetime(2026, 9, 29, 2, 59, 59, 999000, tzinfo=UTC)),
        ("1790650800000", datetime(2026, 9, 29, 3, 0, 0, tzinfo=UTC)),
        ("1790650800001", datetime(2026, 9, 29, 3, 0, 0, 1000, tzinfo=UTC)),
    ],
)
def test_okx_event_ts_keeps_milliseconds_across_a_minute_boundary(ts_ms, expected):
    frame = OKX_FRAME.replace(OKX_FRAME_TS, f'"ts":"{ts_ms}"')
    assert frame != OKX_FRAME
    _, (ws,) = parse_okx(frame, LINEAR)
    (rest,) = parse_okx_data(json.loads(frame)["data"], LINEAR, "rest")
    assert ws.event_ts == expected
    assert rest.event_ts == expected
    assert ws.event_ts.utcoffset() == timedelta(0)
    assert rest.event_ts.utcoffset() == timedelta(0)


def test_okx_event_ts_survives_the_recorder_buffer_unchanged():
    # R:148 only checks the venue and the coverage count of the same frame.
    recorder = LiquidationRecorder(lambda *a, **k: None, clock=lambda: 1790650762.25)
    assert recorder.on_okx_frame(OKX_FRAME) == "data"
    (liq,) = recorder.buffer.liquidations
    assert liq.event_ts == datetime(2026, 9, 29, 2, 59, 21, 954000, tzinfo=UTC)
    assert liq.event_ts.utcoffset() == timedelta(0)


# --- F4: the last 5 s before a minute ends were never probed --------------------
#
# `CoverageTracker.confirmed()` releases minute M once `M + 60 <= last_proof`
# (ingestion/liquidations.py:667). The existing test proves every 10 s up to
# T0+55 and then jumps to T0+60, so any threshold in (T0+55, T0+60] passes:
# `M + 59 < last_proof` (S2, equivalent under whole seconds) and `M + 56 <=`
# are both green. Under the production `time.time()` S2 confirms a minute up
# to 1 s before it ends. The proofs here are sub-second, none on a minute edge.


def _flowing_tracker(**kwargs):
    tracker = CoverageTracker(**kwargs)
    tracker.data()
    return tracker


def _chain(tracker, start, end, step=9.7):
    """Proofs at start, start+step, ... while <= end (never whole seconds)."""
    t = start
    while t <= end:
        tracker.proof(t)
        t += step


BLIND_BAND = [56.0, 57.4, 58.9, 59.5, 59.999, 59.99999]


@pytest.mark.parametrize("offset", BLIND_BAND)
def test_a_proof_in_the_last_seconds_of_a_minute_does_not_confirm_it(offset):
    tracker = _flowing_tracker()
    _chain(tracker, T0 - 5.3, T0 + 55.0)  # last proof T0+52.9: the chain is alive
    assert tracker.last_proof == pytest.approx(T0 + 52.9)
    assert tracker.confirmed() == []
    tracker.proof(T0 + offset)  # < 5 s before the minute ends, within max_silence
    assert tracker.confirmed() == []


def test_the_minute_is_confirmed_by_the_first_proof_at_or_after_its_end():
    tracker = _flowing_tracker()
    _chain(tracker, T0 - 5.3, T0 + 55.0)
    tracker.proof(T0 + 59.99999)
    assert tracker.confirmed() == []
    tracker.proof(T0 + 60.0)  # exactly the end: proven (the `<=` of the rule)
    assert tracker.confirmed() == [(T0, 0)]
    assert tracker.confirmed() == []


def test_a_later_minute_obeys_the_same_threshold_to_the_sub_second():
    tracker = _flowing_tracker()
    _chain(tracker, T0 - 5.3, T0 + 63.0)  # last proof T0+62.6
    assert tracker.confirmed() == [(T0, 0)]
    _chain(tracker, T0 + 70.1, T0 + 119.0)
    tracker.proof(T0 + 119.5)
    assert tracker.confirmed() == []
    tracker.proof(T0 + 119.999)
    assert tracker.confirmed() == []
    tracker.proof(T0 + 120.001)
    assert tracker.confirmed() == [(T0 + 60, 0)]


# --- F5: the fake clock only ever returned whole seconds ------------------------
#
# Every `clock.t` and `proof(...)` in the suite was `T0 +- int`; production uses
# `time.time()`, always fractional. `CoverageTracker.proof` decides whether a
# stretch starts mid-minute with `t % 60` (liquidations.py:644). Under `int(t)`
# (S3a) a stretch starting at M+0.x looks like it starts exactly at M and
# claims minute M, over-claiming a minute that was alive for < 1 s of it. The
# recorder takes its time from `self._clock()` (liquidation_recorder.py:307 on
# the acked-frame path, and the Binance and pong paths), so `int(self._clock())`
# (S3b) hides behind a whole-second fake the same way.


@pytest.mark.parametrize("offset", [0.001, 0.5, 0.999, 30.25, 59.999])
def test_a_stretch_starting_inside_a_minute_first_confirms_the_next_whole_minute(
    offset,
):
    tracker = _flowing_tracker()
    _chain(tracker, T0 + offset, T0 + 131.0)  # last proof lands in T0+120..T0+130
    assert tracker.last_proof >= T0 + 120
    assert [m for m, _ in tracker.confirmed()] == [T0 + 60]


@pytest.mark.parametrize("offset", [0.0, -0.001, -5.3])
def test_a_stretch_alive_at_the_minute_start_claims_that_minute(offset):
    tracker = _flowing_tracker()
    _chain(tracker, T0 + offset, T0 + 70.0)  # last proof lands in T0+60..T0+70
    assert tracker.last_proof >= T0 + 60
    assert [m for m, _ in tracker.confirmed()] == [T0]


def test_counts_at_fractional_receive_times_land_in_their_own_minute():
    tracker = _flowing_tracker()
    tracker.proof(T0 - 5.3)
    tracker.count(T0 - 0.001)  # before the first claimed minute: not attributed
    tracker.count(T0 + 59.999)
    tracker.count(T0 + 60.0, 2)  # the next minute's first instant
    tracker.count(T0 + 119.999, 4)
    _chain(tracker, T0 + 4.4, T0 + 131.0)
    assert tracker.confirmed() == [(T0, 1), (T0 + 60, 6)]


class FractionalClock:
    """Like time.time(): refuses to be read on a whole second."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        assert self.t != int(self.t), f"whole-second clock reading {self.t}"
        return self.t


def _fractional_recorder():
    sink = Sink()
    clock = FractionalClock(T0 - 100.3)
    return LiquidationRecorder(sink, clock=clock), clock, sink


OPENING_FRAME = {
    "binance": lambda r: r.on_binance_frame(BINANCE_FRAME),
    "okx": lambda r: r.on_okx_frame(OKX_SUBSCRIBED),
    "bybit": lambda r: r.on_bybit_frame(BYBIT_SUBSCRIBED),
}


def _coverage_written(sink):
    return [row for batch in sink.batches for row in batch[1]]


def _flush(recorder):
    return asyncio.run(recorder.flush())


def _pongs(recorder, clock, venue, first, last, step=10.0):
    t = first
    while t <= last:
        clock.t = t
        recorder.on_pong(venue)
        t += step


@pytest.mark.parametrize("venue", sorted(OPENING_FRAME))
def test_a_connection_opening_mid_second_does_not_claim_the_minute_it_opened_in(
    venue,
):
    # The Sink must see no coverage for T0 (alive for 59.5 s of it) while the
    # same timeline does hand over T0+60 once that has fully elapsed.
    recorder, clock, sink = _fractional_recorder()
    clock.t = T0 + 0.5
    OPENING_FRAME[venue](recorder)
    _pongs(recorder, clock, venue, T0 + 10.5, T0 + 60.5)
    assert _flush(recorder)
    assert _coverage_written(sink) == []
    _pongs(recorder, clock, venue, T0 + 70.5, T0 + 120.5)
    assert _flush(recorder)
    assert _coverage_written(sink) == [(venue, T0 + 60, 0)]


@pytest.mark.parametrize("venue", sorted(OPENING_FRAME))
def test_a_pong_that_restarts_the_chain_mid_second_does_not_claim_its_minute(venue):
    recorder, clock, sink = _fractional_recorder()
    OPENING_FRAME[venue](recorder)  # a proof at T0-100.3
    _pongs(recorder, clock, venue, T0 + 0.5, T0 + 60.5)  # > 30 s later: new stretch
    assert _flush(recorder)
    assert _coverage_written(sink) == []
    _pongs(recorder, clock, venue, T0 + 70.5, T0 + 120.5)
    assert _flush(recorder)
    assert _coverage_written(sink) == [(venue, T0 + 60, 0)]


# --- F6: the uid dedupe window was only pinned against an 8-row fixture ---------
#
# `_kraken_seen_uids` keeps the last KRAKEN_SEEN_UID_CAP uids so a window that
# the API returns again is not folded into the minute aggregate twice
# (liquidation_recorder.py:98, :138). The history endpoint returns up to 100
# rows, so the cap MUST hold at least one full window. The existing dedupe test
# polls an 8-row fixture twice, so any cap >= 8 passed (S4: cap 50 was green).
# The aggregate is written by an ADDITIVE upsert (storage/liquidation_store.py:
# 57-69), so a re-folded trade silently inflates trade_count and total_size.
#
# What is pinned: a 100-row window never double-counts, however it slides, and
# the set stays bounded. What is NOT pinned: the value 400 itself (a margin of
# four windows is a choice, not a correctness boundary; see the audit note).

KBASE = datetime(2026, 10, 6, 1, 0, 0, tzinfo=UTC)
API_WINDOW = 100  # rows per history response (liquidations.py:28)


def _kraken_trade_at(i):
    when = KBASE + timedelta(seconds=0.37 + 0.9 * i)  # sub-second, 2 minutes / 134
    return {
        "time": when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "uid": f"t{i:05d}",
        "price": 100.0 + i % 7,
        "size": 1.0,
        "side": "buy",
        "type": "fill",
    }


def _truth_by_minute(indices):
    """{minute: trade count}, from the test's own arithmetic (no production)."""
    out = Counter()
    for i in indices:
        when = KBASE + timedelta(seconds=0.37 + 0.9 * i)
        out[int(when.replace(second=0, microsecond=0).timestamp())] += 1
    return out


def _window(end):
    """The API's answer when trades 0..end-1 exist: newest first, <= 100 rows."""
    newest_first = reversed(range(max(0, end - API_WINDOW), end))
    return {"history": [_kraken_trade_at(i) for i in newest_first]}


def _folded_by_minute(recorder, symbol="PF_XBTUSD"):
    out = Counter()
    for agg in recorder.buffer.kraken_minutes:
        if agg.symbol == symbol:
            out[agg.minute] += agg.trade_count
    return out


def _kraken_recorder():
    return LiquidationRecorder(lambda *a, **k: None, clock=lambda: 1790650800.5)


def test_a_full_100_row_window_polled_again_is_folded_once():
    recorder = _kraken_recorder()
    payload = _window(API_WINDOW)
    assert len(payload["history"]) == API_WINDOW
    for _ in range(3):
        recorder.process_kraken_response("PF_XBTUSD", payload)
    assert _folded_by_minute(recorder) == _truth_by_minute(range(API_WINDOW))
    assert sum(_folded_by_minute(recorder).values()) == API_WINDOW


def test_sliding_100_row_windows_fold_every_trade_exactly_once():
    recorder = _kraken_recorder()
    total = 260
    ends = sorted(set(range(7, total + 1, 7)) | {total})
    for end in ends:
        recorder.process_kraken_response("PF_XBTUSD", _window(end))
    assert recorder.kraken_gaps["PF_XBTUSD"] == 0  # the windows overlap: no gap
    folded = _folded_by_minute(recorder)
    assert folded == _truth_by_minute(range(total))
    assert sum(folded.values()) == total
    # The additive columns the upsert sums follow the same count.
    assert sum(a.total_size for a in recorder.buffer.kraken_minutes) == pytest.approx(
        float(total)
    )


def test_the_seen_set_is_bounded_and_always_holds_the_latest_window():
    recorder = _kraken_recorder()
    total = 10 * API_WINDOW
    assert total > KRAKEN_SEEN_UID_CAP  # the loop below must overflow the cap
    for end in range(API_WINDOW, total + 1, API_WINDOW):
        recorder.process_kraken_response("PF_XBTUSD", _window(end))
    seen = recorder._kraken_seen_uids["PF_XBTUSD"]
    assert len(seen) == KRAKEN_SEEN_UID_CAP
    latest = {row["uid"] for row in _window(total)["history"]}
    assert latest <= set(seen)
    assert sum(_folded_by_minute(recorder).values()) == total


def test_each_symbol_keeps_its_own_seen_set():
    recorder = _kraken_recorder()
    payload = _window(API_WINDOW)
    recorder.process_kraken_response("PF_XBTUSD", payload)
    recorder.process_kraken_response("PF_ETHUSD", payload)  # same uids, other symbol
    assert sum(_folded_by_minute(recorder, "PF_XBTUSD").values()) == API_WINDOW
    assert sum(_folded_by_minute(recorder, "PF_ETHUSD").values()) == API_WINDOW
