"""KI-069 against a real Postgres: /health's database section keeps one
deadline even when the database answers connects but not queries.

The unit tests pin the arithmetic against fakes; only a real server can show
that `statement_timeout` covers a query waiting on a lock, and that nothing
else in the request stacks its own bound on top. "Reachable but slow" is made
by holding ACCESS EXCLUSIVE on the two tables the reader scans — connects and
`SELECT 1` still answer instantly, the reads block. A stopped Postgres shows
nothing here (the gate short-circuits), which is why the ticket was missed.

The locks are held for about a second. Pointed at a live database, writers
to `signals` and `world_events` wait that long; point DATABASE_URL at a
disposable one.
"""

import time
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from storage import postgres_store
from storage.db import get_pool, ping

pytestmark = pytest.mark.skipif(not ping(), reason="Postgres unavailable")

# Both tests use a 1s budget: small enough to run fast, and below the old
# code's 2s per-statement bound, so a regression to independent bounds fails
# on elapsed time rather than passing slowly.
_BUDGET = 1.0
_SLACK = 0.5


@contextmanager
def _reads_blocked():
    with psycopg.connect(get_pool().conninfo) as locker:
        locker.execute("SET lock_timeout = '2s'")
        locker.execute("LOCK TABLE signals, world_events IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            locker.rollback()


def test_the_reader_stops_at_its_deadline_when_queries_block():
    with _reads_blocked():
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            postgres_store.world_liveness_times(timeout_seconds=_BUDGET)
        elapsed = time.monotonic() - started

    assert elapsed < _BUDGET + _SLACK


def test_health_answers_within_its_budget_when_queries_block(monkeypatch):
    # Writes on, so postgres_status() pays its real ping — the full request
    # path the watchdog sees, not a patched gate. DATABASE_URL only needs to
    # be present for postgres_readable(); the pool was built at collection.
    from api.main import app

    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    monkeypatch.setenv("DATABASE_URL", get_pool().conninfo)
    client = TestClient(app)

    with patch("api.main._HEALTH_DB_BUDGET_SECONDS", _BUDGET), _reads_blocked():
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["postgres"]["connected"] is True
    assert data["world"] is None
    assert data["world_unavailable_reason"] == "reader_error"
    assert elapsed < _BUDGET + _SLACK
