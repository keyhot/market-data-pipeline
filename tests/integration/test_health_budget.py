"""KI-069 against a real Postgres: /health's database section keeps one
deadline even when the database answers connects but not queries.

The unit tests pin the arithmetic against fakes; only a real server can show
that `statement_timeout` covers a query waiting on a lock, and that nothing
else in the request stacks its own bound on top. "Reachable but slow" is made
by holding ACCESS EXCLUSIVE on the tables a read touches — connects and
`SELECT 1` still answer instantly, the reads block. A stopped Postgres shows
nothing here (the gate short-circuits), which is why the ticket was missed.

OPT-IN, because of those locks: this module runs only when
`POSTGRES_DISPOSABLE=1` says the database behind the pool may be locked. With
no DATABASE_URL the pool falls back to localhost:5432, which on the streaming
box is the live database, and the dev/prod guard does not help — the locker
connects outside the pool, and LOCK is permitted in a read-only transaction
anyway. CI sets the variable for its throwaway service container; locally,
point DATABASE_URL at a disposable Postgres and set it too.
"""

import os
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from storage import db, postgres_store
from storage.db import get_pool, ping

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("POSTGRES_DISPOSABLE") != "1",
        reason="takes ACCESS EXCLUSIVE locks; set POSTGRES_DISPOSABLE=1 "
        "against a disposable database",
    ),
    pytest.mark.skipif(not ping(), reason="Postgres unavailable"),
]

# A 1s budget for the /health tests: small enough to run fast, and below the
# old code's 2s per-statement bound, so a regression to independent bounds
# fails on elapsed time rather than passing slowly.
_BUDGET = 1.0
_SLACK = 0.5


@contextmanager
def _blocked(*tables: str):
    with psycopg.connect(get_pool().conninfo) as locker:
        locker.execute("SET lock_timeout = '2s'")
        locker.execute(f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            locker.rollback()


def test_the_reader_stops_at_its_deadline_when_queries_block():
    with _blocked("signals", "world_events"):
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

    with (
        patch("api.main._HEALTH_DB_BUDGET_SECONDS", _BUDGET),
        _blocked("signals", "world_events"),
    ):
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["postgres"]["connected"] is True
    assert data["world"] is None
    assert data["world_unavailable_reason"] == "reader_error"
    assert elapsed < _BUDGET + _SLACK


def test_the_cold_role_probe_gives_up_on_a_slow_database():
    # The review's 6.93s: a cold process probes deployment_identity before the
    # pool exists, holding `_pool_lock`, and connect_timeout ends at the
    # handshake. With the table locked the probe must give up at its own
    # bound and read as "no claim" — the guard's existing answer for a
    # database it could not ask. Run in a thread so a regression fails here
    # instead of hanging until the lock is released.
    conninfo = get_pool().conninfo
    result: dict = {}

    def probe():
        started = time.monotonic()
        result["role"] = db._database_role(conninfo)
        result["elapsed"] = time.monotonic() - started

    with _blocked("deployment_identity"):
        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(timeout=db._ROLE_PROBE_TIMEOUT_SECONDS + 2.0)
        finished = not worker.is_alive()

    assert finished, "role probe still blocked on a locked deployment_identity"
    assert result["role"] is None
    assert result["elapsed"] < db._ROLE_PROBE_TIMEOUT_SECONDS + _SLACK
