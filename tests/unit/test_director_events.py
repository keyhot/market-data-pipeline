"""Director actions become append-only world events, so the log records what
the director did. Registry-subset invariant + build shapes + the JSONL spool
that survives a Postgres outage."""

from datetime import datetime

from director import events as director_events
from director.events import (
    DIRECTOR_EVENT_TYPES,
    build_commentary_spoken,
    build_scene_switched,
    record_director_events,
)
from world.reactions import REACTIONS
from world.salience import KNOWN_EVENT_TYPES


def test_director_event_types_registered_everywhere():
    assert DIRECTOR_EVENT_TYPES <= KNOWN_EVENT_TYPES
    assert DIRECTOR_EVENT_TYPES <= set(REACTIONS)  # every type renders something


def test_build_scene_switched_shape():
    e = build_scene_switched("world-focus", "chart-focus")
    assert e["event_type"] == "scene_switched"
    assert e["payload"] == {"scene": "world-focus", "from": "chart-focus"}
    assert e["symbol"] is None and e["severity"] == 1.0
    assert isinstance(e["occurred_at"], datetime)


def test_build_commentary_spoken_carries_text_and_source_event():
    e = build_commentary_spoken("optimist", "Called it!", event_id=42, symbol="BTCUSDT")
    assert e["event_type"] == "commentary_spoken"
    assert e["payload"]["character"] == "optimist"
    assert e["payload"]["text"] == "Called it!"
    assert e["payload"]["event_id"] == 42  # provable link back to the real event
    assert e["symbol"] == "BTCUSDT"


def test_record_spools_on_db_failure(tmp_path, monkeypatch):
    spool = tmp_path / "d.jsonl"

    def boom(events):
        raise RuntimeError("pg down")

    monkeypatch.setattr(director_events, "append_world_events", boom)
    record_director_events(
        [build_scene_switched("world-focus", "chart-focus")], spool_path=spool
    )
    assert spool.exists()
    assert len(spool.read_text().strip().splitlines()) == 1


def test_record_flushes_spool_then_appends(tmp_path, monkeypatch):
    spool = tmp_path / "d.jsonl"
    # DB down -> the first event spools.
    monkeypatch.setattr(
        director_events,
        "append_world_events",
        lambda e: (_ for _ in ()).throw(RuntimeError("down")),
    )
    record_director_events(
        [build_commentary_spoken("anxious", "uh oh", event_id=1)], spool_path=spool
    )
    assert spool.exists()
    # DB back -> the spooled backlog flushes AND the new event appends; spool gone.
    appended = []
    monkeypatch.setattr(
        director_events, "append_world_events", lambda e: appended.extend(e)
    )
    record_director_events(
        [build_scene_switched("event-focus", "chart-focus")], spool_path=spool
    )
    assert not spool.exists()
    types = [e["event_type"] for e in appended]
    assert "commentary_spoken" in types and "scene_switched" in types


def test_record_empty_flushes_backlog_without_error(tmp_path, monkeypatch):
    # An empty tick should still drain any backlog (flush-on-tick).
    spool = tmp_path / "d.jsonl"
    appended = []
    monkeypatch.setattr(
        director_events, "append_world_events", lambda e: appended.extend(e)
    )
    record_director_events([], spool_path=spool)  # no spool file -> no-op, no crash
    assert appended == []
