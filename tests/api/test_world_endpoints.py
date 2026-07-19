import asyncio
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import _world_event_stream, app

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
    with patch("api.main.get_world_events", return_value=[_event(1)]):
        response = client.get("/world/events")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    assert data["events"][0]["event_type"] == "big_move"


def test_world_events_404_when_empty():
    with patch("api.main.get_world_events", return_value=[]):
        assert client.get("/world/events").status_code == 404


def test_world_events_503_when_postgres_down():
    with patch("api.main.get_world_events", side_effect=RuntimeError("down")):
        assert client.get("/world/events").status_code == 503


def test_world_events_rejects_unknown_type_and_bad_symbol():
    assert client.get("/world/events?event_type=alien_invasion").status_code == 400
    assert client.get("/world/events?symbol=%3Cscript%3E").status_code == 400
    assert client.get("/world/events?since=notadate").status_code == 400


def test_world_events_passes_filters():
    with patch("api.main.get_world_events", return_value=[_event(1)]) as mock:
        client.get("/world/events?event_type=streak&symbol=btcusdt&limit=5")

    kwargs = mock.call_args.kwargs
    assert kwargs["event_type"] == "streak"
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["limit"] == 5


def test_signals_endpoint_includes_accuracy():
    with (
        patch("api.main.get_signals", return_value=[_signal_row()]),
        patch("api.main.get_signal_accuracy", return_value=_accuracy()),
    ):
        response = client.get("/signals/BTCUSDT")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    assert data["accuracy"]["streak_outcome"] == "loss"


def test_signals_404_when_empty():
    with (
        patch("api.main.get_signals", return_value=[]),
        patch("api.main.get_signal_accuracy", return_value=_accuracy()),
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

    with patch("api.main.get_world_events", side_effect=fake):
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

    with patch("api.main.get_world_events", side_effect=RuntimeError("down")):
        assert asyncio.run(first_chunk()) == ": keepalive\n\n"
