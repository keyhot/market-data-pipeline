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


# --- KI-011: flush isolation + honest exception logging ---


def test_poison_spool_does_not_block_fresh_events(tmp_path, monkeypatch, caplog):
    # KI-011: a single un-appendable row in the spool MUST NOT block a fresh
    # event from being persisted on the same tick. The current code wraps
    # _flush_spool + append_world_events(events) in one try, so any spool-row
    # failure takes down the new event too. Fix: isolate the flush so the
    # new-event append runs unconditionally.
    import json
    import logging
    spool = tmp_path / "d.jsonl"
    # Pre-populate with one row that will explode on append.
    spool.write_text(
        json.dumps({
            "occurred_at": "2026-07-27T12:00:00+00:00",
            "event_type": "scene_switched",
            "severity": 1.0,
            "symbol": None,
            "payload": {"scene": "broken", "from": None},
        }) + "\n"
    )
    import director.events as mod

    captured: list[list[dict]] = []
    raises_left = {"n": 1}

    def sometimes(events):
        captured.append(list(events))
        if raises_left["n"] > 0:
            raises_left["n"] -= 1
            raise RuntimeError("uq_world_events_natural: duplicate key")

    monkeypatch.setattr(mod, "append_world_events", sometimes)

    with caplog.at_level(logging.WARNING, logger="director.events"):
        mod.record_director_events(
            [build_scene_switched("world-focus", "chart-focus")],
            spool_path=spool,
        )

    # Fresh event reached Postgres on this tick, despite the poison spool row.
    fresh_marker = any(
        e["payload"].get("scene") == "world-focus"
        for batch in captured
        for e in batch
    )
    assert fresh_marker, (
        f"fresh scene_switched was never appended; captured={captured}"
    )
    # An honest log line carries the real exception type (in extra / exc_info),
    # not the misleading "Postgres unreachable" label.
    flush_warning = next(
        (r for r in caplog.records if r.message == "director_event_spool_flush_failed"),
        None,
    )
    assert flush_warning is not None, (
        f"expected honest flush-failure log, got: "
        f"{[r.message for r in caplog.records]!r}"
    )
    assert flush_warning.error_type is RuntimeError, (
        f"expected the real exception class, got {flush_warning.error_type!r}"
    )
    assert "duplicate" in flush_warning.error_message
    assert not any(
        "Postgres unreachable" in r.message for r in caplog.records
    ), f"misleading label still produced: {[r.message for r in caplog.records]!r}"


def test_fresh_events_spool_when_db_unreachable(tmp_path, monkeypatch):
    # Behavioural preservation: a real Postgres-down still spools the FRESH
    # events. KI-011 must not regress this contract.
    import director.events as mod

    spool = tmp_path / "d.jsonl"

    def always_down(events):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mod, "append_world_events", always_down)

    mod.record_director_events(
        [build_scene_switched("world-focus", "chart-focus")], spool_path=spool
    )
    assert spool.exists()
    contents = spool.read_text()
    assert "world-focus" in contents
