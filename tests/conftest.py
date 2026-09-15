# tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def postgres_writes_off_by_default(monkeypatch):
    """POSTGRES_WRITE_ENABLED defaults on in production; unit/API tests run
    offline, so force it off. Tests that exercise the write path re-enable it
    with monkeypatch.setenv.

    DATABASE_URL goes with it (KI-079): with writes off, postgres_readable()
    deliberately falls through to DATABASE_URL and pings it, so a developer box
    that exports one makes real connections from the offline suite — 7.22s
    against 1.29s for tests/api/test_health_endpoint.py, measured. The fixture
    owns every variable the offline path reads, not one of the two."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _refuse_database(*args, **kwargs):
    raise RuntimeError(
        "the offline test suite opens no database connections (KI-083); "
        "tests that need Postgres live in tests/integration"
    )


_SPOOLS = {
    "STREAM_EVENT_SPOOL": "stream_events.spool.jsonl",
    "DIRECTOR_EVENT_SPOOL": "director_events.spool.jsonl",
    "BROADCAST_EVENT_SPOOL": "broadcast_events.spool.jsonl",
}


@pytest.fixture(autouse=True)
def no_database_outside_integration(request, monkeypatch, tmp_path):
    """KI-083: unsetting DATABASE_URL never made the suite offline, because
    storage.db falls back to localhost:5432 — on the streaming box, the
    production database. Two watchdog tests dispatch a real `tick`'s actions,
    so every full run appended a `stream_dropped renderer_blank` to the
    append-only world log and the live director said "We're dropping frames"
    on air.

    So connections are refused where they are made — the pool and the role
    probe in storage.db — and the pool singleton is cleared, because collecting
    tests/integration builds it against the real URL before any fixture runs.
    Callers already degrade on a refused connection: `ping()` reads False,
    event writers spool. The spools move to tmp, because the production
    services flush `data/*.spool.jsonl` from the checkout they run in, and a
    refused write spooled there would reach production one restart later.

    tests/integration is exempt: it is the suite that talks to Postgres, and
    it skips itself when there is none."""
    if "integration" in request.node.path.parts:
        return
    import storage.db as db

    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "ConnectionPool", _refuse_database)
    monkeypatch.setattr(db.psycopg, "connect", _refuse_database)
    for var, name in _SPOOLS.items():
        monkeypatch.setenv(var, str(tmp_path / name))
