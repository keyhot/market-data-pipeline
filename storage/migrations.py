"""The migration ledger (KI-091): which `scripts/migrate_NNN.sql` a database has.

RECORDING ONLY. Nothing here applies DDL and nothing here writes: a runner
that applied migrations would be a tool for writing to whatever database it
was pointed at, and "is every migration file on disk also recorded in the
database" needs no such tool. The table is `schema_migrations(version,
applied_at)`; a fresh volume gets its rows from `db/init.sql` (which carries
the net effect of every migration file), and a future migration appends its
own `INSERT INTO schema_migrations (version) VALUES ('NNN') ON CONFLICT
(version) DO NOTHING`. The migration files that already exist are not edited.

KI-090 is why this exists: `migrate_013` (price_bars.volume BIGINT ->
NUMERIC) was believed shipped for two months while one database still had
BIGINT, and nothing recorded which database had which.

`check_migration_ledger()` runs once at API startup and only logs. It must
never stop the app from booting, so it raises nothing; and it must work where
the table is not there yet, which is the live database until the owner
creates it — that case is ONE warning naming the gap.

Versions are the digits in the file name (`migrate_013.sql` -> `"013"`),
compared as text and ordered as numbers.
"""

import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path

import psycopg

import storage.db as db

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_MIGRATION_FILE = re.compile(r"migrate_(\d+)\.sql")


def _version_key(version: str) -> tuple[int, str]:
    # 9 < 20 < 100, whatever the zero-padding; ties (009 vs 9) stay stable.
    return (int(version), version) if version.isdigit() else (-1, version)


def on_disk_versions(scripts_dir: Path | None = None) -> list[str]:
    """The versions of the `migrate_NNN.sql` files in `scripts_dir`, in order."""
    directory = SCRIPTS_DIR if scripts_dir is None else Path(scripts_dir)
    if not directory.is_dir():
        return []
    versions = {
        match.group(1)
        for path in directory.iterdir()
        if (match := _MIGRATION_FILE.fullmatch(path.name))
    }
    return sorted(versions, key=_version_key)


def missing_migrations(applied: Iterable[str], on_disk: Iterable[str]) -> list[str]:
    """The on-disk versions the ledger does not record, oldest first.

    Pure. A recorded version with no file (pruned, renumbered) is not a gap in
    the database, so only the files are checked against the ledger.
    """
    recorded = set(applied)
    return sorted(
        {version for version in on_disk if version not in recorded},
        key=_version_key,
    )


def read_applied_versions(url: str) -> set[str] | None:
    """The versions recorded in `schema_migrations`, or None if there is no
    such table.

    One SELECT on a read-only, time-bounded connection (`db.connect_read_only`,
    the dev/prod guard's own options): it cannot write, whatever it is
    pointed at. Any other failure — unreachable, a query that is cancelled —
    propagates; only "no such table" is an answer.
    """
    with db.connect_read_only(url) as conn:
        try:
            rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        except psycopg.errors.UndefinedTable:
            # The failed statement left the transaction aborted; end it so
            # leaving the connection has nothing to commit.
            conn.rollback()
            return None
    return {str(version) for (version,) in rows}


def _names(versions: Iterable[str]) -> str:
    return ", ".join(f"migrate_{version}" for version in versions)


def check_migration_ledger(scripts_dir: Path | None = None) -> list[str]:
    """Log what the ledger says about the database in DATABASE_URL; return it.

    Silent when every migration file on disk is recorded. One warning naming
    the unrecorded files when some are not; one warning naming the gap when
    the table is absent; one warning when the database cannot be read. Never
    raises and never writes, and with no DATABASE_URL it does nothing — the
    default URL is localhost:5432, which on the streaming box is the live
    database, and an offline run has none to check.
    """
    url = os.environ.get(db.DATABASE_URL_ENV)
    if not url:
        return []
    try:
        on_disk = on_disk_versions(scripts_dir)
        applied = read_applied_versions(url)
        if applied is None:
            problems = [
                "migration ledger: the schema_migrations table does not exist "
                "in this database, so which scripts/migrate_*.sql have run "
                "here is unrecorded (KI-091); the ledger has not been created. "
                f"On disk: {_names(on_disk) or 'none'}"
            ]
        else:
            missing = missing_migrations(applied, on_disk)
            problems = (
                [
                    "migration ledger: not recorded as applied in this "
                    f"database: {_names(missing)} (KI-091); either the "
                    "migration has not been run here or the ledger was not "
                    "backfilled"
                ]
                if missing
                else []
            )
    except Exception as e:
        problems = [
            "migration ledger: could not be read, so nothing was checked "
            f"({type(e).__name__}: {e})"
        ]
    for problem in problems:
        logger.warning("%s", problem)
    return problems
