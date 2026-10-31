"""KI-091 against a real Postgres: the migration ledger records, warns, and
never writes.

The unit tests (tests/unit/test_migration_ledger.py) pin the comparison and
drive the reader through fakes; only a server can show that a database built
from `db/init.sql` really records every migration file, that a withheld row is
named, and — the case the live database is in today — that a database with NO
`schema_migrations` table logs one warning and carries on.

OPT-IN, like test_health_budget.py: it runs only when `POSTGRES_DISPOSABLE=1`
says the server behind DATABASE_URL may be written to. Unlike that module it
does not fall back to localhost:5432 (on the streaming box, the live
database) and it does not even connect unless both are set. Every test builds
its own throwaway database on that server with CREATE DATABASE, applies
`db/init.sql` to it, and drops it afterwards, so nothing in the database the
URL names is touched.
"""

import logging
import os
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

import storage.db as db
import storage.migrations as migrations
from storage.migrations import (
    check_migration_ledger,
    on_disk_versions,
    read_applied_versions,
)

_URL = os.environ.get("MDP_TEST_DATABASE_URL", "")
_INIT_SQL = Path(__file__).resolve().parents[2] / "db" / "init.sql"


def _disposable() -> bool:
    # The flag is read before any connection is made: no probe of a database
    # nobody said was disposable.
    # Opted in means opted in: no connection is made here, so a service
    # container that is down fails the tests that need it (at their fixture)
    # instead of skipping the whole module green.
    return os.environ.get("POSTGRES_DISPOSABLE") == "1" and bool(_URL)


pytestmark = pytest.mark.skipif(
    not _disposable(),
    reason="set POSTGRES_DISPOSABLE=1 and MDP_TEST_DATABASE_URL to a disposable "
    "Postgres (it creates and drops its own databases)",
)


@pytest.fixture
def ledger_db(monkeypatch):
    """A fresh database built from db/init.sql — what a new volume gets."""
    name = f"mdp_test_ledger_{uuid.uuid4().hex[:12]}"
    url = make_conninfo(_URL, dbname=name)
    with psycopg.connect(_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(_INIT_SQL.read_text())
            monkeypatch.setenv("DATABASE_URL", url)
            yield url
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _execute(url: str, statement: str):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(statement)


def _ledger_rows(url: str):
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


class _Records(logging.Handler):
    """Records on the ledger's own logger. Not caplog: importing api.main runs
    config.logging's dictConfig, which replaces the root handlers — including
    caplog's — if that import first happens inside the test."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def ledger_logs():
    handler = _Records()
    ledger_logger = logging.getLogger("storage.migrations")
    previous = ledger_logger.level
    ledger_logger.setLevel(logging.DEBUG)
    ledger_logger.addHandler(handler)
    try:
        yield handler
    finally:
        ledger_logger.removeHandler(handler)
        ledger_logger.setLevel(previous)


def test_a_fresh_volume_records_every_migration_file(ledger_db):
    assert read_applied_versions(ledger_db) == set(on_disk_versions())


def test_every_recorded_version_has_a_timestamp(ledger_db):
    rows = _ledger_rows(ledger_db)
    assert rows and all(applied_at is not None for _, applied_at in rows)


def test_a_fully_recorded_database_is_silent(ledger_db, ledger_logs):
    assert check_migration_ledger() == []
    assert _warnings(ledger_logs) == []


def test_with_013_withheld_the_warning_names_migrate_013(ledger_db, ledger_logs):
    _execute(ledger_db, "DELETE FROM schema_migrations WHERE version = '013'")
    check_migration_ledger()
    records = _warnings(ledger_logs)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "migrate_013" in message
    assert "migrate_012" not in message and "migrate_014" not in message


def test_the_check_does_not_heal_the_ledger(ledger_db, ledger_logs):
    # Recording only: finding a gap must not write the row that closes it.
    _execute(ledger_db, "DELETE FROM schema_migrations WHERE version = '013'")
    before = _ledger_rows(ledger_db)
    check_migration_ledger()
    assert _ledger_rows(ledger_db) == before


def test_an_absent_table_warns_once_raises_nothing_and_creates_nothing(
    ledger_db, ledger_logs
):
    _execute(ledger_db, "DROP TABLE schema_migrations")
    returned = check_migration_ledger()
    records = _warnings(ledger_logs)
    assert len(records) == 1 and returned == [records[0].getMessage()]
    message = records[0].getMessage()
    # The gap's own message — not the generic "could not be read" one, which
    # a re-raise from the reader would also produce and which also says
    # 'relation "schema_migrations" does not exist'.
    assert "does not exist" in message and "KI-091" in message
    assert "has not been created" in message
    assert "could not be read" not in message
    with psycopg.connect(ledger_db) as conn:
        (regclass,) = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
    assert regclass is None, "the check must not create the ledger"


def test_reading_an_absent_table_is_none_not_an_error(ledger_db):
    _execute(ledger_db, "DROP TABLE schema_migrations")
    assert read_applied_versions(ledger_db) is None


def test_the_reader_connection_cannot_write(ledger_db):
    with db.connect_read_only(ledger_db) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO schema_migrations (version) VALUES ('999')")
    assert "999" not in read_applied_versions(ledger_db)


def test_startup_logs_the_gap_and_carries_on(ledger_db, ledger_logs, monkeypatch):
    # The whole path the live database takes: the app boots against a database
    # whose ledger table does not exist and still answers.
    _execute(ledger_db, "DROP TABLE schema_migrations")
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("WS_INGEST_ENABLED", "0")
    from api.main import app

    with TestClient(app) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _warnings(ledger_logs):
            time.sleep(0.05)
        assert client.get("/openapi.json").status_code == 200

    records = _warnings(ledger_logs)
    assert any("has not been created" in r.getMessage() for r in records)
    assert not any(r.name == migrations.__name__ and r.exc_info for r in records), (
        "the gap is a warning, not a traceback"
    )
