import asyncio
import inspect
import json
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from api.routers.sse import _world_event_stream
from storage import postgres_store

client = TestClient(app)


def _event(event_id, event_type="big_move", severity=4.2):
    return {
        "id": event_id,
        "occurred_at": "2026-07-19T10:00:00+00:00",
        "event_type": event_type,
        "symbol": "BTCUSDT",
        "severity": severity,
        "payload": {"sigmas": severity},
    }


def _signal_row():
    return {
        "signal_timestamp": "2026-07-19T10:00:00+00:00",
        "model_version": "test-v0",
        "horizon_bars": 15,
        "direction": "up",
        "probability": 0.8,
        "resolved_at": None,
        "outcome": None,
    }


def _accuracy():
    return {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "window": 50,
        "resolved": 1,
        "wins": 0,
        "losses": 1,
        "hit_rate": 0.0,
        "current_streak": 1,
        "streak_outcome": "loss",
    }


def test_world_events_returns_rows():
    with patch("api.routers.world.get_world_events", return_value=[_event(1)]):
        response = client.get("/world/events")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    assert data["events"][0]["event_type"] == "big_move"


def test_world_events_404_when_empty():
    with patch("api.routers.world.get_world_events", return_value=[]):
        assert client.get("/world/events").status_code == 404


def test_world_events_503_when_postgres_down():
    with patch("api.routers.world.get_world_events", side_effect=RuntimeError("down")):
        assert client.get("/world/events").status_code == 503


def test_world_events_rejects_unknown_type_and_bad_symbol():
    assert client.get("/world/events?event_type=alien_invasion").status_code == 400
    assert client.get("/world/events?symbol=%3Cscript%3E").status_code == 400
    assert client.get("/world/events?since=notadate").status_code == 400


def test_world_events_passes_filters():
    with patch("api.routers.world.get_world_events", return_value=[_event(1)]) as mock:
        client.get("/world/events?event_type=streak&symbol=btcusdt&limit=5")

    kwargs = mock.call_args.kwargs
    assert kwargs["event_type"] == "streak"
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["limit"] == 5


def test_signals_endpoint_includes_accuracy():
    with (
        patch("api.routers.stored.get_signals", return_value=[_signal_row()]),
        patch("api.routers.stored.get_signal_accuracy", return_value=_accuracy()),
    ):
        response = client.get("/signals/BTCUSDT")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    assert data["accuracy"]["streak_outcome"] == "loss"


def test_signals_404_when_empty():
    with (
        patch("api.routers.stored.get_signals", return_value=[]),
        patch("api.routers.stored.get_signal_accuracy", return_value=_accuracy()),
    ):
        assert client.get("/signals/BTCUSDT").status_code == 404


def test_signals_rejects_bad_symbol():
    assert client.get("/signals/%3Cscript%3E").status_code == 400


def test_world_stream_emits_only_new_events():
    feeds = [
        [_event(5)],                  # at connect: swallowed, sets baseline
        [_event(6), _event(5)],       # one new event (newest first)
        [_event(6), _event(5)],       # nothing new
    ]

    def fake(limit):
        return feeds.pop(0) if feeds else [_event(6), _event(5)]

    async def collect():
        stream = _world_event_stream(poll_seconds=0.01)
        chunks = [await anext(stream) for _ in range(4)]
        await stream.aclose()
        return chunks

    with patch("api.routers.sse.get_world_events", side_effect=fake):
        chunks = asyncio.run(collect())

    data_chunks = [c for c in chunks if c.startswith("data: ")]
    assert len(data_chunks) == 1
    emitted = json.loads(data_chunks[0][len("data: "):])
    assert emitted["id"] == 6


def test_world_stream_survives_db_errors():
    async def first_chunk():
        stream = _world_event_stream(poll_seconds=0.01)
        first = await anext(stream)
        await stream.aclose()
        return first

    with patch("api.routers.sse.get_world_events", side_effect=RuntimeError("down")):
        assert asyncio.run(first_chunk()) == ": keepalive\n\n"


def _at(hour, minute, second=0):
    return datetime(2026, 11, 2, hour, minute, second, tzinfo=timezone.utc)


class _FakeWorldEventsTable:
    """world_events as the database keeps it: `id` is an identity column
    (assigned at insert, only ever rises), `occurred_at` is the caller's.
    `get_world_events` copies the real getter's ORDER BY and LIMIT."""

    def __init__(self):
        self._rows = []
        self._next_id = 1

    def insert(self, occurred_at, event_type="signal_resolved"):
        event_id = self._next_id
        self._next_id += 1
        self._rows.append((event_id, occurred_at, event_type))
        return event_id

    def get_world_events(self, limit=50):
        # ORDER BY occurred_at DESC, id DESC LIMIT %s
        newest_first = sorted(
            self._rows, key=lambda row: (row[1], row[0]), reverse=True
        )
        return [
            {
                "id": event_id,
                "occurred_at": occurred.isoformat(),
                "event_type": event_type,
                "symbol": "BTCUSDT",
                "severity": 1.0,
                "payload": {},
            }
            for event_id, occurred, event_type in newest_first[:limit]
        ]


def _table_with_a_back_dated_row_above_the_newest_one():
    """ids 1-5 are bar-stamped, id 6 is stamped 'now' (newest by occurred_at),
    id 7 is a signal_resolved the resolver wrote later but stamped with its
    older target time: the highest id sits BELOW row 0 of the window."""
    table = _FakeWorldEventsTable()
    for minute in range(46, 51):
        table.insert(_at(11, minute), "volatility_spike")
    table.insert(_at(12, 0, 10), "scene_switched")
    table.insert(_at(11, 59), "signal_resolved")
    return table


def _emitted_ids(table, polls, after_poll=None):
    """Connect to the stream, let it poll `polls` times, return the ids it
    emitted. `after_poll(n)` runs after the n-th poll, like a writer inserting
    between two polls."""

    async def collect():
        stream = _world_event_stream(poll_seconds=0.001)
        emitted, polled = [], 0
        async for chunk in stream:
            if chunk.startswith("data: "):
                emitted.append(json.loads(chunk[len("data: "):])["id"])
                continue
            polled += 1
            if after_poll is not None:
                after_poll(polled)
            if polled >= polls:
                break
        await stream.aclose()
        return emitted

    with patch("api.routers.sse.get_world_events", side_effect=table.get_world_events):
        return asyncio.run(collect())


def test_fake_table_copies_the_real_order_by_with_the_highest_id_below_row_zero():
    assert "ORDER BY occurred_at DESC, id DESC LIMIT %s" in inspect.getsource(
        postgres_store.get_world_events
    )
    window = _table_with_a_back_dated_row_above_the_newest_one().get_world_events(20)
    assert [event["id"] for event in window][:2] == [6, 7]
    assert max(event["id"] for event in window) == 7


def test_world_stream_reconnect_does_not_resend_a_back_dated_pre_connect_event():
    table = _table_with_a_back_dated_row_above_the_newest_one()

    assert _emitted_ids(table, polls=4) == []
    assert _emitted_ids(table, polls=4) == []  # a reconnect: still nothing


def test_world_stream_emits_a_new_back_dated_event_once_and_no_pre_connect_one():
    table = _table_with_a_back_dated_row_above_the_newest_one()

    def writer_inserts_after_first_poll(polled):
        if polled == 1:
            table.insert(_at(11, 58))  # id 8, back-dated: sorts below id 6 and 7

    emitted = _emitted_ids(table, polls=6, after_poll=writer_inserts_after_first_poll)

    assert emitted == [8]


def test_world_stream_advances_the_watermark_to_the_highest_id_it_emitted():
    table = _table_with_a_back_dated_row_above_the_newest_one()

    def writer_inserts_two_between_polls(polled):
        if polled == 1:
            table.insert(_at(12, 5))  # id 8, newest by occurred_at
            table.insert(_at(11, 58))  # id 9, back-dated: emitted before id 8

    emitted = _emitted_ids(table, polls=6, after_poll=writer_inserts_two_between_polls)

    assert sorted(emitted) == [8, 9]  # each once; id 9 is not re-sent after id 8
