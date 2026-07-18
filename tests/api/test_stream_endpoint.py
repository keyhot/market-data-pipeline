import asyncio
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import _bar_event_stream, app

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

    with patch("api.main.get_price_bars", side_effect=fake):
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

    with patch("api.main.get_price_bars", side_effect=boom):
        first = asyncio.run(first_chunk())

    assert first == ": keepalive\n\n"
