"""Stream lifecycle events: severity mapping, dropped-event cooldown, and the
JSONL spool that keeps history through the exact failures it records."""

from datetime import datetime, timedelta, timezone

import pytest

from world import stream_events
from world.salience import KNOWN_EVENT_TYPES


def test_stream_types_registered_in_known_event_types():
    assert stream_events.STREAM_EVENT_TYPES <= KNOWN_EVENT_TYPES


def test_severity_semantics():
    assert stream_events.SEVERITIES == {
        "stream_started": 1.0,
        "stream_stopped": 2.0,
        "stream_dropped": 5.0,
        # KI-021: an ingest reconnect really broke and really fixed itself —
        # more notable than a stop we chose, less than an outage.
        "stream_reconnected": 3.0,
    }


def test_build_event_shape():
    event = stream_events.build_stream_event("stream_dropped", {"reason": "x"})
    assert event["event_type"] == "stream_dropped"
    assert event["severity"] == 5.0
    assert event["payload"] == {"reason": "x"}
    assert "symbol" not in event  # stream events are symbol-less
    assert event["occurred_at"].tzinfo is not None


def test_build_rejects_unknown_type():
    with pytest.raises(ValueError):
        stream_events.build_stream_event("stream_exploded")


def test_record_appends(monkeypatch, tmp_path):
    written = []
    monkeypatch.setattr(
        stream_events, "append_world_events", lambda evs: written.extend(evs)
    )
    monkeypatch.setattr(stream_events, "latest_world_event_time", lambda *a: None)
    event = stream_events.record_stream_event(
        "stream_started", spool_path=tmp_path / "spool.jsonl"
    )
    assert written and event is not None


def test_dropped_cooldown_suppresses_flapping(monkeypatch, tmp_path):
    written = []
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        stream_events, "append_world_events", lambda evs: written.extend(evs)
    )
    monkeypatch.setattr(
        stream_events,
        "latest_world_event_time",
        lambda *a: now - timedelta(minutes=1),
    )
    result = stream_events.record_stream_event(
        "stream_dropped", spool_path=tmp_path / "spool.jsonl"
    )
    assert result is None and written == []


def test_started_never_cooldown_suppressed(monkeypatch, tmp_path):
    written = []
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        stream_events, "append_world_events", lambda evs: written.extend(evs)
    )
    monkeypatch.setattr(
        stream_events,
        "latest_world_event_time",
        lambda *a: now - timedelta(seconds=10),
    )
    result = stream_events.record_stream_event(
        "stream_started", spool_path=tmp_path / "spool.jsonl"
    )
    assert result is not None and len(written) == 1


def test_spool_on_db_failure_then_flush(monkeypatch, tmp_path):
    spool = tmp_path / "spool.jsonl"

    def broken(evs):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(stream_events, "append_world_events", broken)
    monkeypatch.setattr(stream_events, "latest_world_event_time", lambda *a: None)
    event = stream_events.record_stream_event(
        "stream_dropped", {"reason": "obs_unreachable"}, spool_path=spool
    )
    assert event is not None
    assert spool.exists() and len(spool.read_text().strip().splitlines()) == 1

    written = []
    monkeypatch.setattr(
        stream_events, "append_world_events", lambda evs: written.extend(evs)
    )
    flushed = stream_events.flush_spool(spool)
    assert flushed == 1
    assert written[0]["event_type"] == "stream_dropped"
    assert written[0]["occurred_at"].tzinfo is not None
    assert not spool.exists()  # flushed spool is gone


def test_record_flushes_spool_first(monkeypatch, tmp_path):
    spool = tmp_path / "spool.jsonl"
    spool.write_text(
        '{"occurred_at": "2026-07-20T00:00:00+00:00", "event_type": '
        '"stream_dropped", "severity": 5.0, "payload": {}}\n'
    )
    written = []
    monkeypatch.setattr(
        stream_events, "append_world_events", lambda evs: written.extend(evs)
    )
    monkeypatch.setattr(stream_events, "latest_world_event_time", lambda *a: None)
    stream_events.record_stream_event("stream_started", spool_path=spool)
    assert len(written) == 2  # spooled event + fresh event
