"""The live world-events writer must accept symbol-less events. scene_switched
and stream_* set `symbol` to None *explicitly*, so `.get("symbol", "")` returns
None (the default only fires when the key is absent) and `None.upper()` used to
crash the whole batch — silently blocking the director's world-memory writes and,
via the flush-before-append path, every event queued behind the bad row."""

from datetime import datetime, timezone

import storage.postgres_store as ps


def _capture_rows(monkeypatch):
    captured = {}

    def fake_executemany(sql, rows):
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr(ps, "_executemany", fake_executemany)
    return captured


def test_append_world_events_normalizes_explicit_none_symbol(monkeypatch):
    captured = _capture_rows(monkeypatch)
    events = [
        {
            "occurred_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
            "event_type": "scene_switched",
            "symbol": None,  # explicit None — the case that crashed
            "severity": 1.0,
            "payload": {"scene": "world-focus"},
        },
        {
            "occurred_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
            "event_type": "commentary_spoken",
            "symbol": "btcusdt",
            "severity": 1.0,
            "payload": {},
        },
    ]
    assert ps.append_world_events(events) == 2
    symbols = [row[2] for row in captured["rows"]]
    assert symbols == [None, "BTCUSDT"]  # None -> NULL; real symbol uppercased


def test_append_world_events_handles_absent_symbol_key(monkeypatch):
    captured = _capture_rows(monkeypatch)
    ps.append_world_events(
        [
            {
                "occurred_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
                "event_type": "stream_started",
                "severity": 1.0,
                "payload": {},
            }
        ]
    )
    assert captured["rows"][0][2] is None
