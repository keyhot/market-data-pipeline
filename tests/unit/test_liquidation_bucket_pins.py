"""Pins for audit-5 F1 and F2: which minute a Kraken trade lands in, and what
the storage layer is handed.

F1. `aggregate_kraken_minutes` buckets by `_floor_minute(t.event_ts.timestamp())`
    (ingestion/liquidations.py:511). The result becomes `kraken_trade_minutes.minute`,
    the column R3-b joins against. Nothing asserted the key, so a bucket one
    minute late, or floor swapped for round, stayed green. Every expected
    minute below is a literal `datetime(..., tzinfo=utc)`, never `_floor_minute`
    or a helper built on it, or the mutant would hide behind its own maths.
    Kraken liquidations are not bucketed: `kraken_liquidations` forwards
    `event_ts` untouched. They share the bucket code only through `liq_count`
    and `liq_size` on the aggregate, and the same `_floor_minute` feeds the
    Kraken coverage cursor and the WS venues' `CoverageTracker`.

F2. Every recorder test hands the writer a fake sink, so the epoch -> timestamptz
    conversion in storage/liquidation_store.py (:76, :131, :143, and the
    `event_ts` pass-through at :92) had no test. The columns are TIMESTAMPTZ
    (db/init.sql:148, :183, :194, :210). psycopg sends an aware datetime as
    timestamptz (`DatetimeDumper`, psycopg/types/datetime.py:153) and a naive
    one as plain `timestamp` (`DatetimeNoTzDumper`, :163), which Postgres then
    reads in the SESSION time zone. So the contract is: every stamp handed to
    the pool is an aware UTC datetime. Here the real `write_liquidation_batch`
    runs against a fake pool that captures what `executemany` receives and
    models that server-side cast. The host zone is varied in-process as well,
    because a dropped `tz=` produces a naive LOCAL time that is only wrong
    when the host is not UTC.

No database: `get_pool` is replaced on the module that uses it.
"""

import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ingestion.liquidation_recorder import LiquidationRecorder
from ingestion.liquidations import (
    CoverageTracker,
    aggregate_kraken_minutes,
    kraken_coverage_minutes,
    kraken_liquidations,
    parse_binance,
    parse_kraken_history,
)
from storage import liquidation_store
from tests.unit.test_liquidations import BINANCE_FRAME, KRAKEN_PAYLOAD

UTC = timezone.utc

# --- the host zone ------------------------------------------------------------

# zone -> UTC offset in seconds on 2026-10-05, so a zone the box cannot load
# (the TZ variable then silently means UTC) fails loudly instead of passing.
HOST_ZONES = {
    "UTC": 0,
    "Europe/Warsaw": 2 * 3600,
    "America/Los_Angeles": -7 * 3600,
    "Asia/Kolkata": 5 * 3600 + 30 * 60,
}
_PROBE = int(datetime(2026, 10, 5, 15, 49, tzinfo=UTC).timestamp())


@pytest.fixture(params=list(HOST_ZONES), autouse=True)
def host_tz(request):
    """Run every test in this file under each host time zone, then put TZ back.

    Restored by hand, not through monkeypatch: this fixture's teardown runs
    before monkeypatch's, so a `tzset()` here would re-read the patched value
    and leave the rest of the suite in the wrong zone.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    try:
        assert time.localtime(_PROBE).tm_gmtoff == HOST_ZONES[request.param]
        yield request.param
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def _utc(*parts, micro=0):
    return datetime(*parts, microsecond=micro, tzinfo=UTC)


def _epoch(*parts):
    """Epoch seconds of a whole-minute literal."""
    return int(_utc(*parts).timestamp())


# --- F1: the bucket minute ----------------------------------------------------


def _row(uid, stamp, size, kind="fill"):
    return {
        "time": stamp,
        "uid": uid,
        "price": 100.0,
        "size": size,
        "side": "sell",
        "type": kind,
    }


# Each size is a distinct power of two, so a bucket's `total_size` names exactly
# which trades are in it. Liquidations sit on both sides of a boundary.
EDGE_ROWS = [
    _row("a", "2026-10-05T15:48:59.999000Z", 1.0),
    _row("b", "2026-10-05T15:48:59.999999Z", 2.0, "liquidation"),
    _row("c", "2026-10-05T15:49:00.000000Z", 4.0, "liquidation"),
    _row("d", "2026-10-05T15:49:00.000001Z", 8.0),
    _row("e", "2026-10-05T15:49:59.999999Z", 16.0),
    _row("f", "2026-10-05T15:59:59.999000Z", 256.0),
    _row("g", "2026-10-05T16:00:00.000000Z", 32.0),  # the next hour
    _row("h", "2026-10-05T23:59:59.999000Z", 64.0, "liquidation"),
    _row("i", "2026-10-06T00:00:00.000000Z", 128.0),  # the next day
]
ETH_ROW = _row("eth-1", "2026-10-05T15:49:00.000000Z", 0.5)

# (symbol, minute) -> (trade_count, liq_count, total_size, liq_size)
EXPECTED_EDGE_BUCKETS = {
    ("PF_XBTUSD", _utc(2026, 10, 5, 15, 48)): (2, 1, 3.0, 2.0),
    ("PF_XBTUSD", _utc(2026, 10, 5, 15, 49)): (3, 1, 28.0, 4.0),
    ("PF_XBTUSD", _utc(2026, 10, 5, 15, 59)): (1, 0, 256.0, 0.0),
    ("PF_XBTUSD", _utc(2026, 10, 5, 16, 0)): (1, 0, 32.0, 0.0),
    ("PF_XBTUSD", _utc(2026, 10, 5, 23, 59)): (1, 1, 64.0, 64.0),
    ("PF_XBTUSD", _utc(2026, 10, 6, 0, 0)): (1, 0, 128.0, 0.0),
    ("PF_ETHUSD", _utc(2026, 10, 5, 15, 49)): (1, 0, 0.5, 0.0),
}


def _edge_trades():
    return parse_kraken_history({"history": EDGE_ROWS}, "PF_XBTUSD") + (
        parse_kraken_history({"history": [ETH_ROW]}, "PF_ETHUSD")
    )


def test_kraken_trades_land_in_the_minute_they_fall_in():
    aggs = aggregate_kraken_minutes(_edge_trades())
    got = {
        (a.symbol, datetime.fromtimestamp(a.minute, tz=UTC)): (
            a.trade_count,
            a.liq_count,
            a.total_size,
            a.liq_size,
        )
        for a in aggs
    }
    assert len(aggs) == len(EXPECTED_EDGE_BUCKETS)  # no bucket split or merged
    assert got == EXPECTED_EDGE_BUCKETS


def test_the_bucket_key_is_the_minute_start_not_the_trade_time():
    aggs = aggregate_kraken_minutes(_edge_trades())
    assert all(a.minute % 60 == 0 for a in aggs)
    assert all(isinstance(a.minute, int) for a in aggs)


def test_kraken_liquidations_keep_their_exact_timestamp():
    # Not bucketed: the row carries the print's own time, to the microsecond.
    liqs = kraken_liquidations(parse_kraken_history({"history": EDGE_ROWS}, "PF_X"))
    assert [x.event_ts for x in liqs] == [
        _utc(2026, 10, 5, 15, 48, 59, micro=999999),
        _utc(2026, 10, 5, 15, 49, 0, micro=0),
        _utc(2026, 10, 5, 23, 59, 59, micro=999000),
    ]


def test_the_real_fixture_window_buckets_to_its_two_minutes():
    aggs = aggregate_kraken_minutes(parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD"))
    assert {a.minute: a.trade_count for a in aggs} == {
        _epoch(2026, 10, 5, 15, 48): 4,
        _epoch(2026, 10, 5, 15, 49): 4,
    }


def test_the_recorder_buffers_the_aggregate_under_those_same_minutes():
    recorder = LiquidationRecorder(lambda *a, **k: None, clock=lambda: 0.0)
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    assert {a.minute: a.trade_count for a in recorder.buffer.kraken_minutes} == {
        _epoch(2026, 10, 5, 15, 48): 4,
        _epoch(2026, 10, 5, 15, 49): 4,
    }


def _history(*stamps):
    rows = [_row(f"u{i}", s, 1.0) for i, s in enumerate(stamps)]
    return parse_kraken_history({"history": rows}, "PF_XBTUSD")


M1548 = _epoch(2026, 10, 5, 15, 48)
M1549 = _epoch(2026, 10, 5, 15, 49)
M1550 = _epoch(2026, 10, 5, 15, 50)
M1559 = _epoch(2026, 10, 5, 15, 59)
M1600 = _epoch(2026, 10, 5, 16, 0)
M2358 = _epoch(2026, 10, 5, 23, 58)
M2359 = _epoch(2026, 10, 5, 23, 59)
M0000 = _epoch(2026, 10, 6, 0, 0)


@pytest.mark.parametrize(
    ("cursor", "stamps", "gap", "expected"),
    [
        pytest.param(  # :59.999999 is still inside the open minute
            M1548,
            ["2026-10-05T15:48:30Z", "2026-10-05T15:48:59.999999Z"],
            False,
            ([], M1548),
            id="newest-trade-at-59.999999-keeps-its-minute-open",
        ),
        pytest.param(  # :00.000000 opens the next minute and closes this one
            M1548,
            ["2026-10-05T15:48:59.999999Z", "2026-10-05T15:49:00.000000Z"],
            False,
            ([(M1548, True, 1)], M1549),
            id="newest-trade-at-00.000000-closes-the-previous-minute",
        ),
        pytest.param(  # the hour edge
            M1559,
            ["2026-10-05T15:59:59.999999Z", "2026-10-05T16:00:00.000000Z"],
            False,
            ([(M1559, True, 1)], M1600),
            id="across-the-hour",
        ),
        pytest.param(  # the day edge, two minutes claimed at once
            M2358,
            [
                "2026-10-05T23:58:59.999999Z",
                "2026-10-05T23:59:59.999999Z",
                "2026-10-06T00:00:00.000000Z",
            ],
            False,
            ([(M2358, True, 1), (M2359, True, 1)], M0000),
            id="across-the-day",
        ),
        pytest.param(  # a gap reaches through the oldest trade's minute
            M1548 - 60,
            ["2026-10-05T15:48:59.999999Z"],
            True,
            ([(M1548 - 60, False, 0), (M1548, False, 1)], M1549),
            id="gap-oldest-trade-at-59.999999",
        ),
        pytest.param(
            M1548 - 60,
            ["2026-10-05T15:49:00.000000Z"],
            True,
            ([(M1548 - 60, False, 0), (M1548, False, 0), (M1549, False, 1)], M1550),
            id="gap-oldest-trade-at-00.000000",
        ),
    ],
)
def test_kraken_coverage_claims_a_minute_only_once_it_has_closed(
    cursor, stamps, gap, expected
):
    assert kraken_coverage_minutes(cursor, _history(*stamps), gap) == expected


def test_kraken_coverage_first_poll_starts_the_cursor_after_the_open_minute():
    on_boundary = _history("2026-10-05T15:49:00.000000Z")
    just_before = _history("2026-10-05T15:48:59.999999Z")
    assert kraken_coverage_minutes(None, on_boundary, False) == ([], M1550)
    assert kraken_coverage_minutes(None, just_before, False) == ([], M1549)


def test_the_ws_coverage_tracker_counts_a_row_in_its_receive_minute():
    tracker = CoverageTracker(max_silence_s=600)
    tracker.data()
    tracker.proof(float(M1548))  # a stretch that starts exactly on a minute
    tracker.count(M1548 + 59.999)
    tracker.count(M1549 + 0.0)
    tracker.count(M1549 + 59.999999, n=2)
    tracker.count(M1550 + 0.0)
    tracker.proof(float(M1550 + 60))
    assert tracker.confirmed() == [(M1548, 1), (M1549, 3), (M1550, 1)]


def test_a_stretch_that_starts_at_59_999_first_claims_the_following_minute():
    tracker = CoverageTracker(max_silence_s=600)
    tracker.data()
    tracker.proof(M1548 + 59.999)  # minute 15:48 is not whole; 15:49 is
    tracker.proof(float(M1550 + 30))
    assert tracker.confirmed() == [(M1549, 0)]


# --- F2: what storage is handed ----------------------------------------------


class _Cursor:
    def __init__(self, captured):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, rows):
        table = re.search(r"INSERT INTO\s+(\w+)", sql).group(1)
        self._captured.setdefault(table, []).extend(list(rows))


class _Connection:
    closed = False

    def __init__(self, captured):
        self._captured = captured

    def cursor(self):
        return _Cursor(self._captured)


class FakePool:
    """Captures what `write_liquidation_batch` hands to `executemany`, per table."""

    def __init__(self):
        self.captured = {}

    @contextmanager
    def connection(self):
        yield _Connection(self.captured)


def stored_instant(value, session_tz):
    """The instant Postgres keeps for a value bound to a timestamptz column.

    An aware datetime travels as `timestamptz` and keeps its instant. A naive
    one travels as `timestamp` and the server reads it in the session TimeZone.
    `session_tz` is that server setting. It is independent of the host's TZ.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(session_tz))
    return value.astimezone(UTC)


SESSION_ZONES = ["UTC", "Europe/Warsaw", "America/New_York"]

# Where each table's timestamp sits in the row `write_liquidation_batch` binds.
STAMP_AT = {
    "liquidations": 2,  # (venue, symbol, event_ts, ...)
    "liquidation_coverage": 1,  # (venue, minute, rows_received)
    "kraken_trade_minutes": 1,  # (symbol, minute, ...)
    "liquidation_coverage_kraken": 1,  # (symbol, minute, covered, rows_received)
}

BINANCE_TS = datetime.fromtimestamp(1790650455.232, tz=UTC)
COVERAGE_MINUTES = [_utc(2026, 10, 5, 15, 48), _utc(2026, 10, 6, 0, 0)]
KRAKEN_COVERAGE_MINUTES = [_utc(2026, 10, 5, 23, 59), _utc(2026, 10, 6, 0, 1)]

EXPECTED_STAMPS = {
    "liquidations": [
        BINANCE_TS,
        _utc(2026, 10, 5, 15, 48, 59, micro=999999),
        _utc(2026, 10, 5, 15, 49, 0),
        _utc(2026, 10, 5, 23, 59, 59, micro=999000),
    ],
    "liquidation_coverage": COVERAGE_MINUTES,
    "kraken_trade_minutes": [minute for _, minute in EXPECTED_EDGE_BUCKETS],
    "liquidation_coverage_kraken": KRAKEN_COVERAGE_MINUTES,
}


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    # The name `storage.liquidation_store` bound at import, not `storage.db`.
    monkeypatch.setattr(liquidation_store, "get_pool", lambda: fake)
    return fake


def _write(fake):
    kraken_trades = parse_kraken_history({"history": EDGE_ROWS}, "PF_XBTUSD")
    liquidations = parse_binance(BINANCE_FRAME) + kraken_liquidations(kraken_trades)
    liquidation_store.write_liquidation_batch(
        liquidations,
        [("binance", int(t.timestamp()), n) for t, n in zip(COVERAGE_MINUTES, (3, 0))],
        aggregate_kraken_minutes(_edge_trades()),
        [
            ("PF_XBTUSD", int(t.timestamp()), covered, n)
            for t, covered, n in zip(KRAKEN_COVERAGE_MINUTES, (True, False), (5, 0))
        ],
    )
    for table in STAMP_AT:
        assert fake.captured.get(table), f"nothing reached {table}"
    return fake.captured


@pytest.mark.parametrize("table", list(STAMP_AT))
def test_every_stamp_handed_to_storage_is_aware_utc(pool, table):
    rows = _write(pool)[table]
    stamps = [row[STAMP_AT[table]] for row in rows]
    for stamp in stamps:
        assert stamp.tzinfo is not None, "a naive datetime reaches a TIMESTAMPTZ"
        assert stamp.utcoffset() == timedelta(0)
    assert sorted(stamps) == sorted(EXPECTED_STAMPS[table])


@pytest.mark.parametrize("session_tz", SESSION_ZONES)
def test_storage_keeps_the_true_instant_in_any_session_time_zone(pool, session_tz):
    captured = _write(pool)
    for table, at in STAMP_AT.items():
        kept = sorted(stored_instant(row[at], session_tz) for row in captured[table])
        assert kept == sorted(EXPECTED_STAMPS[table]), table


@pytest.mark.parametrize("session_tz", SESSION_ZONES)
def test_an_edge_trade_reaches_kraken_trade_minutes_at_its_minute(pool, session_tz):
    # F1 and F2 together: the value in the column R3-b joins against.
    rows = _write(pool)["kraken_trade_minutes"]
    # (symbol, minute, min_price, max_price, trade_count, total_size,
    #  size_near_min, size_near_max, liq_count, liq_size)
    got = {
        (r[0], stored_instant(r[1], session_tz)): (r[4], r[8], r[5], r[9])
        for r in rows
    }
    assert got == EXPECTED_EDGE_BUCKETS


def test_the_fakes_conversion_tells_naive_from_aware():
    # Guards the double itself: if it ignored the session zone, the two tests
    # above could not fail for a naive stamp.
    wall = datetime(2026, 10, 5, 15, 48)
    assert stored_instant(wall, "UTC") == _utc(2026, 10, 5, 15, 48)
    assert stored_instant(wall, "Europe/Warsaw") == _utc(2026, 10, 5, 13, 48)
    assert stored_instant(_utc(2026, 10, 5, 15, 48), "Europe/Warsaw") == _utc(
        2026, 10, 5, 15, 48
    )
