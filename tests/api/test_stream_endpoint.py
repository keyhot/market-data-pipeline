import asyncio
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from api.routers.sse import _bar_event_stream

client = TestClient(app)


def _bar(minute, close):
    return {
        "timestamp": f"2026-07-18T09:{minute:02d}:00+00:00",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1,
    }


# The bar stream's window is `get_price_bars(symbol, interval, 10)`: the latest
# `limit` bars, oldest first (the SQL is ORDER BY bar_timestamp DESC LIMIT n,
# then reversed in Python; storage/postgres_store.py). Stubs for it come from
# one helper that applies that same ordering to a table of stored bars, so no
# test can hand the stream an order the real getter never returns.
def _stored_window(table, limit):
    newest_first = sorted(table, key=lambda bar: bar["timestamp"], reverse=True)
    return list(reversed(newest_first[:limit]))


def _fake_get_price_bars(table):
    def fake(symbol, interval, limit):
        return _stored_window(table, limit)

    return fake


async def _poll(stream):
    """One poll of the stream: its data chunks, up to the keepalive that
    closes the poll. The table may be changed between calls."""
    data = []
    while (chunk := await anext(stream)) != ": keepalive\n\n":
        data.append(chunk)
    return data


def _timestamps(data_chunks):
    return [json.loads(c[len("data: "):])["timestamp"] for c in data_chunks]


def _ts(minute):
    return _bar(minute, 1.0)["timestamp"]


def test_stream_rejects_invalid_symbol():
    assert client.get("/stream/bars/%3Cscript%3E").status_code == 400


def test_stream_emits_only_bars_newer_than_connect():
    feeds = [
        [_bar(0, 1.0)],                  # at connect: swallowed, sets baseline
        [_bar(0, 1.0), _bar(1, 2.0)],    # one new bar
        [_bar(0, 1.0), _bar(1, 2.0)],    # nothing new
    ]

    def fake(symbol, interval, limit):
        return feeds.pop(0) if feeds else [_bar(0, 1.0), _bar(1, 2.0)]

    async def collect():
        stream = _bar_event_stream("BTCUSDT", "1m", poll_seconds=0.01)
        chunks = [await anext(stream) for _ in range(4)]
        await stream.aclose()
        return chunks

    with patch("api.routers.sse.get_price_bars", side_effect=fake):
        chunks = asyncio.run(collect())

    assert chunks[0] == ": keepalive\n\n"
    data_chunks = [c for c in chunks if c.startswith("data: ")]
    assert len(data_chunks) == 1
    emitted = json.loads(data_chunks[0][len("data: "):])
    assert emitted["timestamp"] == "2026-07-18T09:01:00+00:00"


def test_stream_survives_db_errors():
    def boom(symbol, interval, limit):
        raise RuntimeError("db down")

    async def first_chunk():
        stream = _bar_event_stream("BTCUSDT", "1m", poll_seconds=0.01)
        first = await anext(stream)
        await stream.aclose()
        return first

    with patch("api.routers.sse.get_price_bars", side_effect=boom):
        first = asyncio.run(first_chunk())

    assert first == ": keepalive\n\n"


def test_stored_window_is_the_latest_bars_oldest_first():
    # Inserted out of order and more than the window holds: the stub returns
    # what ORDER BY bar_timestamp DESC LIMIT n, reversed, returns.
    table = [_bar(m, 1.0) for m in (5, 1, 9, 3, 7, 0, 8, 2, 6, 4)]
    assert [b["timestamp"] for b in _stored_window(table, 3)] == [
        _ts(7), _ts(8), _ts(9)
    ]
    assert _stored_window(table, 99) == [_bar(m, 1.0) for m in range(10)]


def test_stream_connect_window_of_three_bars_emits_none_of_them():
    table = [_bar(0, 1.0), _bar(1, 2.0), _bar(2, 3.0)]

    async def collect():
        stream = _bar_event_stream("BTCUSDT", "1m", poll_seconds=0.01)
        polls = [await _poll(stream) for _ in range(3)]  # connect + 2 unchanged
        await stream.aclose()
        return polls

    fake = _fake_get_price_bars(table)
    with patch("api.routers.sse.get_price_bars", side_effect=fake):
        polls = asyncio.run(collect())

    # The baseline is the newest bar of the window. A baseline at the oldest
    # bar would emit bars 1 and 2 on the second poll; one that never advances
    # past the newest would re-send it on every poll.
    assert polls == [[], [], []]


def test_stream_two_new_bars_in_one_poll_emit_exactly_those_two_in_order():
    table = [_bar(0, 1.0), _bar(1, 2.0), _bar(2, 3.0)]

    async def collect():
        stream = _bar_event_stream("BTCUSDT", "1m", poll_seconds=0.01)
        polls = [await _poll(stream)]  # connect on the three-bar window
        table.extend([_bar(3, 4.0), _bar(4, 5.0)])  # both land between polls
        polls.append(await _poll(stream))
        polls.append(await _poll(stream))  # same table again
        table.append(_bar(5, 6.0))
        polls.append(await _poll(stream))
        await stream.aclose()
        return polls

    fake = _fake_get_price_bars(table)
    with patch("api.routers.sse.get_price_bars", side_effect=fake):
        polls = asyncio.run(collect())

    assert polls[0] == []
    assert _timestamps(polls[1]) == [_ts(3), _ts(4)]  # oldest first, no old bar
    assert polls[2] == []  # nothing is sent twice
    assert _timestamps(polls[3]) == [_ts(5)]  # the watermark moved to the newest
    assert [json.loads(c[len("data: "):]) for c in polls[1]] == [
        _bar(3, 4.0),
        _bar(4, 5.0),
    ]
