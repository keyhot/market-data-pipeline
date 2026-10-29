"""KI-091: the migration ledger is recording only.

Nothing here opens a database: the comparison is pure, the reader is driven
through a fake connection, and the startup check is driven through a fake
reader. The same behaviour against a real (disposable) Postgres is in
tests/integration/test_migration_ledger_postgres.py.

What the ledger must never do is change what a migration does, and it must be
safe where it does not exist yet — the live database has no `schema_migrations`
table today, so "table absent" is the case the check runs in first.
"""

import logging
import re
import threading
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

import storage.db as db
import storage.migrations as migrations
from storage.migrations import (
    check_migration_ledger,
    missing_migrations,
    on_disk_versions,
    read_applied_versions,
)

REPO = Path(__file__).resolve().parents[2]
INIT_SQL = REPO / "db" / "init.sql"
SCRIPTS = REPO / "scripts"


# --- the pure comparison ----------------------------------------------------


class TestMissingMigrations:
    def test_everything_recorded_is_nothing_missing(self):
        assert missing_migrations({"009", "012", "013"}, ["009", "012", "013"]) == []

    def test_a_file_that_is_not_recorded_is_missing(self):
        assert missing_migrations(
            {"009", "012", "014"}, ["009", "012", "013", "014"]
        ) == ["013"]

    def test_an_empty_ledger_misses_every_file(self):
        assert missing_migrations(set(), ["009", "012", "013"]) == ["009", "012", "013"]

    def test_the_first_and_the_last_are_both_found(self):
        # Boundary rows: an off-by-one at either end of the ordering drops one.
        on_disk = ["009", "012", "013", "014", "018"]
        assert missing_migrations({"012", "013", "014"}, on_disk) == ["009", "018"]

    def test_the_answer_is_in_version_order_whatever_order_it_was_given(self):
        assert missing_migrations(set(), ["018", "009", "013", "012"]) == [
            "009",
            "012",
            "013",
            "018",
        ]

    def test_version_order_is_numeric_not_textual(self):
        assert missing_migrations(set(), ["100", "20", "9"]) == ["9", "20", "100"]

    def test_a_recorded_version_with_no_file_is_not_missing(self):
        # A pruned or renumbered file is not a gap in the database.
        assert missing_migrations({"009", "010"}, ["009"]) == []

    def test_a_duplicate_file_name_is_reported_once(self):
        assert missing_migrations(set(), ["013", "013"]) == ["013"]

    def test_a_list_of_applied_versions_works_as_well_as_a_set(self):
        assert missing_migrations(["009"], ["009", "012"]) == ["012"]

    def test_nothing_on_disk_is_nothing_missing(self):
        assert missing_migrations({"009"}, []) == []

    def test_the_comparison_reads_the_ledger(self):
        # The mutant that drops the comparison reports every file as missing.
        assert missing_migrations({"009", "012"}, ["009", "012"]) == []
        assert missing_migrations({"009"}, ["009", "012"]) == ["012"]


# --- which migrations exist -------------------------------------------------


class TestOnDiskVersions:
    def test_reads_the_version_out_of_each_migrate_file(self, tmp_path):
        for name in ("migrate_013.sql", "migrate_009.sql", "migrate_012.sql"):
            (tmp_path / name).write_text("-- x")
        assert on_disk_versions(tmp_path) == ["009", "012", "013"]

    def test_ignores_files_that_are_not_migrations(self, tmp_path):
        for name in (
            "migrate_013.sql",
            "migrate_013.sql.bak",
            "migrate_x.sql",
            "migrate.py",
            "backfill_postgres.py",
            "premigrate_014.sql",
        ):
            (tmp_path / name).write_text("-- x")
        assert on_disk_versions(tmp_path) == ["013"]

    def test_a_missing_directory_is_no_migrations(self, tmp_path):
        assert on_disk_versions(tmp_path / "nope") == []

    def test_the_shipped_migrations_are_the_ones_the_plan_names(self):
        # 010 and 011 never existed; the ledger must not invent them.
        assert on_disk_versions() == [
            "009",
            "012",
            "013",
            "014",
            "015",
            "016",
            "017",
            "018",
        ]


# --- db/init.sql: a fresh volume records what it includes --------------------


def _init_sql() -> str:
    # Code only: the comment above the ledger quotes the INSERT a future
    # migration appends, which is not a statement init.sql runs.
    return re.sub(r"--[^\n]*", "", INIT_SQL.read_text())


class TestInitSql:
    def test_declares_the_ledger_table(self):
        sql = _init_sql()
        match = re.search(
            r"CREATE TABLE (?:IF NOT EXISTS )?schema_migrations\s*\((.*?)\);", sql, re.S
        )
        assert match, "db/init.sql must create schema_migrations"
        body = match.group(1)
        assert re.search(r"version\s+TEXT\s+PRIMARY KEY", body, re.I)
        assert re.search(r"applied_at\s+TIMESTAMPTZ", body, re.I)

    def test_a_fresh_volume_records_every_migration_on_disk(self):
        # init.sql carries the net effect of every migrate_NNN.sql, so a new
        # database must say so. A new migrate file that init.sql does not
        # record is a new volume that warns about itself on its first boot.
        sql = _init_sql()
        match = re.search(
            r"INSERT INTO schema_migrations\s*\(version\)\s*VALUES(.*?)ON CONFLICT",
            sql,
            re.S | re.I,
        )
        assert match, "db/init.sql must record the versions it includes"
        recorded = re.findall(r"'(\d+)'", match.group(1))
        assert sorted(recorded) == on_disk_versions()

    def test_recording_is_idempotent(self):
        assert re.search(
            r"INSERT INTO schema_migrations.*?ON CONFLICT\s*\(version\)\s*DO NOTHING",
            _init_sql(),
            re.S | re.I,
        )

    @pytest.mark.parametrize(
        "version", ["009", "012", "013", "014", "015", "016", "017", "018"]
    )
    def test_the_existing_migrations_do_not_write_the_ledger(self, version):
        # Recording only, no behaviour change: the eight files that existed
        # when the ledger was added are not edited. A LATER migration appends
        # its own INSERT, which is why this names the eight, not the glob.
        text = (SCRIPTS / f"migrate_{version}.sql").read_text()
        assert "schema_migrations" not in text


# --- the reader --------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows=None, raises=None):
        self.sql: list[str] = []
        self.rolled_back = False
        self._rows = rows or []
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rollback(self):
        self.rolled_back = True

    def execute(self, sql, *args):
        self.sql.append(sql)
        if self._raises is not None:
            raise self._raises
        return _Result(self._rows)


def _use_connection(monkeypatch, conn):
    opened: list[dict] = []

    def fake_connect_read_only(url, timeout_seconds=None):
        opened.append({"url": url})
        return conn

    monkeypatch.setattr(db, "connect_read_only", fake_connect_read_only)
    return opened


class TestReadAppliedVersions:
    def test_returns_the_recorded_versions(self, monkeypatch):
        conn = _FakeConnection(rows=[("009",), ("013",)])
        _use_connection(monkeypatch, conn)
        assert read_applied_versions("postgresql://x/y") == {"009", "013"}

    def test_an_empty_ledger_is_an_empty_set_not_an_absent_table(self, monkeypatch):
        _use_connection(monkeypatch, _FakeConnection(rows=[]))
        assert read_applied_versions("postgresql://x/y") == set()

    def test_an_absent_table_reads_as_none_and_raises_nothing(self, monkeypatch):
        conn = _FakeConnection(raises=psycopg.errors.UndefinedTable("no table"))
        _use_connection(monkeypatch, conn)
        assert read_applied_versions("postgresql://x/y") is None
        assert conn.rolled_back, "the aborted transaction must be ended"

    def test_any_other_database_error_is_not_mistaken_for_an_absent_table(
        self, monkeypatch
    ):
        conn = _FakeConnection(raises=psycopg.errors.QueryCanceled("slow"))
        _use_connection(monkeypatch, conn)
        with pytest.raises(psycopg.errors.QueryCanceled):
            read_applied_versions("postgresql://x/y")

    def test_it_only_ever_selects(self, monkeypatch):
        conn = _FakeConnection(rows=[("009",)])
        _use_connection(monkeypatch, conn)
        read_applied_versions("postgresql://x/y")
        assert conn.sql, "it must query the ledger"
        assert all(s.lstrip().upper().startswith("SELECT") for s in conn.sql)

    def test_it_connects_through_the_read_only_guard(self, monkeypatch):
        opened = _use_connection(monkeypatch, _FakeConnection(rows=[]))
        read_applied_versions("postgresql://x/y")
        assert opened == [{"url": "postgresql://x/y"}]


class TestConnectReadOnly:
    def test_opens_with_the_guards_read_only_option_and_a_bound(self, monkeypatch):
        seen: dict = {}

        def fake_connect(url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return object()

        monkeypatch.setattr(db.psycopg, "connect", fake_connect)
        db.connect_read_only("postgresql://x/y")
        assert seen["url"] == "postgresql://x/y"
        assert "default_transaction_read_only=on" in seen["options"]
        assert "statement_timeout=" in seen["options"]
        assert seen["connect_timeout"] == db._ROLE_PROBE_TIMEOUT_SECONDS
        # The same socket bounds as every other connection in the module.
        assert seen["keepalives"] == 1


# --- the startup check -------------------------------------------------------


def _migrations_dir(tmp_path, *versions):
    for v in versions:
        (tmp_path / f"migrate_{v}.sql").write_text("-- x")
    return tmp_path


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture
def db_configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")


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


class TestCheckMigrationLedger:
    def test_names_the_missing_migration(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(
            migrations, "read_applied_versions", lambda url=None: {"009", "012", "014"}
        )
        check_migration_ledger(_migrations_dir(tmp_path, "009", "012", "013", "014"))
        records = _warnings(ledger_logs)
        assert len(records) == 1
        assert "migrate_013" in records[0].getMessage()
        assert "migrate_012" not in records[0].getMessage()

    def test_names_every_missing_migration_in_one_warning(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(
            migrations, "read_applied_versions", lambda url=None: {"009"}
        )
        check_migration_ledger(_migrations_dir(tmp_path, "009", "012", "013"))
        records = _warnings(ledger_logs)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "migrate_012" in message and "migrate_013" in message
        assert message.index("migrate_012") < message.index("migrate_013")

    def test_silent_when_everything_is_recorded(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(
            migrations, "read_applied_versions", lambda url=None: {"009", "012", "013"}
        )
        assert (
            check_migration_ledger(_migrations_dir(tmp_path, "009", "012", "013")) == []
        )
        assert _warnings(ledger_logs) == []

    def test_an_absent_table_logs_one_warning_that_names_the_gap_and_carries_on(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(migrations, "read_applied_versions", lambda url=None: None)
        check_migration_ledger(_migrations_dir(tmp_path, "009", "012", "013"))
        records = _warnings(ledger_logs)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "schema_migrations" in message
        assert "does not exist" in message
        assert "KI-091" in message
        # It is not a claim that anything is missing: with no ledger, nobody
        # knows. The per-file list belongs to the table-present branch.
        assert "not recorded" not in message

    def test_the_absent_table_message_does_not_depend_on_the_files_on_disk(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(migrations, "read_applied_versions", lambda url=None: None)
        check_migration_ledger(tmp_path)  # an image with no scripts/
        assert len(_warnings(ledger_logs)) == 1

    def test_an_unreadable_database_is_one_warning_and_never_an_exception(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        def boom(url=None):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(migrations, "read_applied_versions", boom)
        check_migration_ledger(_migrations_dir(tmp_path, "009"))
        records = _warnings(ledger_logs)
        assert len(records) == 1
        assert "connection refused" in records[0].getMessage()

    def test_nothing_on_disk_is_not_a_warning(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(
            migrations, "read_applied_versions", lambda url=None: {"009"}
        )
        check_migration_ledger(tmp_path)
        assert _warnings(ledger_logs) == []

    def test_no_database_url_means_no_connection_and_no_noise(
        self, monkeypatch, tmp_path, ledger_logs
    ):
        # Ad-hoc local runs and the offline suite have no database at all; the
        # check must not fall back to localhost:5432 (on the streaming box, the
        # live database).
        monkeypatch.delenv("DATABASE_URL", raising=False)

        def never(url=None):
            raise AssertionError("must not read a database it was not given")

        monkeypatch.setattr(migrations, "read_applied_versions", never)
        assert check_migration_ledger(_migrations_dir(tmp_path, "009")) == []
        assert _warnings(ledger_logs) == []

    def test_reads_the_database_it_was_configured_with(
        self, monkeypatch, tmp_path, ledger_logs
    ):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example:5433/d")
        seen = []

        def reader(url=None):
            seen.append(url)
            return set()

        monkeypatch.setattr(migrations, "read_applied_versions", reader)
        check_migration_ledger(_migrations_dir(tmp_path, "009"))
        assert seen == ["postgresql://u:p@db.example:5433/d"]

    def test_returns_what_it_logged(
        self, monkeypatch, tmp_path, db_configured, ledger_logs
    ):
        monkeypatch.setattr(migrations, "read_applied_versions", lambda url=None: set())
        returned = check_migration_ledger(_migrations_dir(tmp_path, "013"))
        assert len(returned) == 1 and "migrate_013" in returned[0]


# --- wiring: startup runs the check and carries on ---------------------------


def test_startup_runs_the_ledger_check(monkeypatch):
    import api.main as main

    ran = threading.Event()
    monkeypatch.setattr(main, "check_migration_ledger", lambda: ran.set() or [])
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("WS_INGEST_ENABLED", "0")

    with TestClient(main.app):
        assert ran.wait(timeout=5), "the lifespan never ran the ledger check"


def test_startup_carries_on_when_the_check_blows_up(monkeypatch):
    import api.main as main

    ran = threading.Event()

    def boom():
        ran.set()
        raise RuntimeError("the ledger check exploded")

    monkeypatch.setattr(main, "check_migration_ledger", boom)
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("WS_INGEST_ENABLED", "0")

    with TestClient(main.app) as client:
        assert ran.wait(timeout=5)
        assert client.get("/openapi.json").status_code == 200
