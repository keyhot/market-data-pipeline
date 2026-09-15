"""The offline-test contract (KI-079).

`tests/conftest.py` promises that unit and API tests run without a live
Postgres.  Writes-off alone does not deliver that: with `POSTGRES_WRITE_ENABLED=0`
`storage.writes.postgres_readable()` falls through to the real `DATABASE_URL`
and pings it, so `/health`'s tests took 7.22s instead of 1.29s on any box that
exports one.  The fixture has to own *both* variables the offline path reads.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _offline_fixture_body():
    """The autouse fixture's undecorated function, so it can be exercised."""
    spec = importlib.util.spec_from_file_location("_conftest_under_test", _CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.postgres_writes_off_by_default
    return fixture._get_wrapped_function()


def test_the_offline_fixture_unsets_database_url():
    body = _offline_fixture_body()
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("DATABASE_URL", "postgresql://unroutable.invalid:5432/nope")
    try:
        body(monkeypatch)
        assert "DATABASE_URL" not in os.environ
    finally:
        monkeypatch.undo()


def test_the_offline_fixture_forces_writes_off():
    body = _offline_fixture_body()
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    try:
        body(monkeypatch)
        assert os.environ["POSTGRES_WRITE_ENABLED"] == "0"
    finally:
        monkeypatch.undo()


# --- KI-083: the offline suite wrote real on-air events -----------------------
#
# Unsetting DATABASE_URL did not make the suite offline: storage.db falls back
# to localhost:5432, which on the streaming box IS the production database.
# Two watchdog tests run `execute_actions` over a real `tick`, so every full run
# appended a `stream_dropped renderer_blank` (detail "frozen", one failure) to
# the append-only world log — 2026-09-15 02:25:09, 06:56:28 and 07:06:40 UTC,
# each within a minute of a suite run, and the live director answered the last
# one on air with "We're dropping frames — is the stream alright?".


def test_the_offline_suite_cannot_reach_a_database():
    import storage.db as db

    with pytest.raises(RuntimeError, match="KI-083"):
        db.get_pool()
    assert db.ping() is False


def test_event_spools_are_isolated_from_the_checkout():
    """A write that fails is spooled, and the production watchdog, director and
    broadcast manager flush their spools from `data/` in the checkout they run
    from — so a refused write spooled there would still reach production, one
    restart later."""
    import storage.db as db

    with pytest.raises(RuntimeError):  # the guard first: never write if it is off
        db.get_pool()
    repo = Path(__file__).resolve().parents[2]
    for var in ("STREAM_EVENT_SPOOL", "DIRECTOR_EVENT_SPOOL", "BROADCAST_EVENT_SPOOL"):
        spool = Path(os.environ[var])
        assert repo not in spool.parents, f"{var} spools into the checkout"


def test_a_stream_event_recorded_offline_lands_in_the_isolated_spool():
    import storage.db as db
    from world import stream_events

    with pytest.raises(RuntimeError):
        db.get_pool()
    stream_events.record_stream_event("stream_started", {"reason": "offline_contract"})
    lines = Path(os.environ["STREAM_EVENT_SPOOL"]).read_text().splitlines()
    assert len(lines) == 1 and "offline_contract" in lines[0]
