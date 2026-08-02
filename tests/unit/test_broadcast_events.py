"""The YouTube broadcast lifecycle becomes append-only world memory, so
"was the stream actually public?" is answerable from the log rather than from
someone's recollection of the Live Control Room. Registry-subset invariant +
build shapes + the JSONL spool that survives a Postgres outage."""

from datetime import datetime, timezone

from broadcast import events as broadcast_events
from broadcast.events import (
    BROADCAST_EVENT_TYPES,
    build_broadcast_created,
    build_broadcast_ended,
    build_broadcast_live,
    record_broadcast_events,
)
from world.reactions import REACTIONS
from world.salience import KNOWN_EVENT_TYPES


def test_broadcast_event_types_registered_everywhere():
    assert BROADCAST_EVENT_TYPES <= KNOWN_EVENT_TYPES
    assert BROADCAST_EVENT_TYPES <= set(REACTIONS)  # every type renders something


def test_build_shapes_are_symbol_free_market_meta_events():
    for build in (build_broadcast_created, build_broadcast_live, build_broadcast_ended):
        event = build("bc-123")
        assert event["symbol"] is None  # a broadcast belongs to no ticker
        assert event["payload"]["broadcast_id"] == "bc-123"
        assert isinstance(event["occurred_at"], datetime)
        assert event["event_type"] in BROADCAST_EVENT_TYPES


def test_ended_outranks_created_and_live_in_severity():
    """Going public is routine; losing the public broadcast is the thing a
    24/7 stream must never do quietly."""
    created = build_broadcast_created("bc-1")
    live = build_broadcast_live("bc-1")
    ended = build_broadcast_ended("bc-1")
    assert ended["severity"] > live["severity"]
    assert created["severity"] == live["severity"] == 1.0


def test_explicit_occurred_at_is_preserved():
    when = datetime(2026, 8, 2, 10, 30, tzinfo=timezone.utc)
    assert build_broadcast_live("bc-1", occurred_at=when)["occurred_at"] == when


def test_record_spools_on_db_failure(tmp_path, monkeypatch):
    spool = tmp_path / "b.jsonl"
    monkeypatch.setattr(
        broadcast_events,
        "append_world_events",
        lambda e: (_ for _ in ()).throw(RuntimeError("pg down")),
    )
    record_broadcast_events([build_broadcast_live("bc-1")], spool_path=spool)
    assert spool.exists()
    assert len(spool.read_text().strip().splitlines()) == 1


def test_record_flushes_spool_then_appends(tmp_path, monkeypatch):
    spool = tmp_path / "b.jsonl"
    monkeypatch.setattr(
        broadcast_events,
        "append_world_events",
        lambda e: (_ for _ in ()).throw(RuntimeError("down")),
    )
    record_broadcast_events([build_broadcast_created("bc-1")], spool_path=spool)
    assert spool.exists()

    appended = []
    monkeypatch.setattr(
        broadcast_events, "append_world_events", lambda e: appended.extend(e)
    )
    record_broadcast_events([build_broadcast_live("bc-1")], spool_path=spool)
    assert not spool.exists()
    types = [e["event_type"] for e in appended]
    assert "broadcast_created" in types and "broadcast_live" in types


def test_poison_spool_does_not_block_fresh_events(tmp_path, monkeypatch):
    """KI-011's rule, inherited: one un-appendable spooled row must never stop
    a fresh broadcast_live from being recorded — on a 24/7 stream that row is
    the difference between a true and a false uptime report."""
    import json

    spool = tmp_path / "b.jsonl"
    spool.write_text(
        json.dumps(
            {
                "occurred_at": "2026-08-01T12:00:00+00:00",
                "event_type": "broadcast_created",
                "severity": 1.0,
                "symbol": None,
                "payload": {"broadcast_id": "poison"},
            }
        )
        + "\n"
    )

    captured: list[list[dict]] = []
    raises_left = {"n": 1}

    def sometimes(events):
        captured.append(list(events))
        if raises_left["n"] > 0:
            raises_left["n"] -= 1
            raise RuntimeError("uq_world_events_natural: duplicate key")

    monkeypatch.setattr(broadcast_events, "append_world_events", sometimes)
    record_broadcast_events([build_broadcast_live("bc-2")], spool_path=spool)

    assert any(
        e["payload"].get("broadcast_id") == "bc-2"
        for batch in captured
        for e in batch
    ), f"fresh broadcast_live never appended; captured={captured}"


def test_record_empty_flushes_backlog_without_error(tmp_path, monkeypatch):
    spool = tmp_path / "b.jsonl"
    appended = []
    monkeypatch.setattr(
        broadcast_events, "append_world_events", lambda e: appended.extend(e)
    )
    record_broadcast_events([], spool_path=spool)  # no spool -> no-op, no crash
    assert appended == []
