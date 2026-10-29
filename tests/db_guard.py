"""KI-106: the only way the test suite may reach a database.

`poetry run pytest` used to run ~20 integration DB tests (17 of them DELETE or
INSERT) against whatever answered on the default URL, localhost:5432 — on the
streaming box, the live database. This module is the one place the rules live;
tests/integration/conftest.py applies them and tests/unit/test_db_guard.py
pins them on fakes.

The rules:
  * Integration tests are opt-in through MDP_TEST_DATABASE_URL. Nothing else
    is consulted: not DATABASE_URL, not PG* variables, not the default URL.
  * The URL names an explicit host and port; the port is not 5432 (a missing
    port means libpq's 5432), and it lists exactly one host.
  * The database name matches ^mdp_test_ both in the URL and as the server
    reports it (`SELECT current_database()`), checked before anything else is
    sent. A mismatch is a refusal, never a skip.
  * Every connection the integration tests open is checked for the port again
    (install_connection_gate), so no pool, probe or raw psycopg.connect can
    land on 5432 even if some other setting is wrong.
Messages never contain the URL itself: it may hold a password.
"""

import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import psycopg
from psycopg.conninfo import conninfo_to_dict

OPT_IN_ENV = "MDP_TEST_DATABASE_URL"
LIVE_PORT = 5432
TEST_DB_RE = re.compile(r"^mdp_test_[a-z0-9_]+$")
CHECK_TIMEOUT_SECONDS = 5

SKIP_REASON = (
    f"integration tests are opt-in: set {OPT_IN_ENV} to a throwaway Postgres "
    "(database mdp_test_*, a port other than 5432). DATABASE_URL and the "
    "localhost:5432 default are never used."
)


class GuardRefusal(RuntimeError):
    """The configured database is not one the tests may touch."""


@dataclass(frozen=True)
class GuardedTarget:
    url: str
    host: str
    port: int
    dbname: str


def _params(conninfo: str, what: str) -> dict[str, str]:
    try:
        params = conninfo_to_dict(conninfo)
    except Exception:
        # `from None`: libpq's parse errors can quote the offending text.
        raise GuardRefusal(
            f"{what} is not a connection string libpq can parse "
            "(value withheld: it may hold a password)"
        ) from None
    if params.get("service"):
        raise GuardRefusal(
            f"{what} uses a libpq service entry, which can supply a host the "
            "guard cannot see; spell out host, port and dbname"
        )
    return params


def _port_of(params: Mapping[str, str], what: str) -> int:
    # libpq routes by hostaddr when both are given, so both are checked.
    host = str(params.get("host") or "").strip()
    hostaddr = str(params.get("hostaddr") or "").strip()
    port_text = str(params.get("port") or "").strip()
    if "," in host or "," in hostaddr or "," in port_text:
        raise GuardRefusal(
            f"{what} lists more than one host or port; a failover list could "
            f"land on the live database, so name exactly one (port not {LIVE_PORT})"
        )
    if not port_text:
        raise GuardRefusal(
            f"{what} has no explicit port, so libpq would use {LIVE_PORT} (or "
            f"PGPORT), the live database's port; name a port other than {LIVE_PORT}"
        )
    if not port_text.isdigit() or not 1 <= (port := int(port_text)) <= 65535:
        raise GuardRefusal(f"{what} has a port that is not a number in 1-65535")
    if port == LIVE_PORT:
        raise GuardRefusal(
            f"{what} points at port {LIVE_PORT}, the live database's port; the "
            f"test suite never connects there. Use a throwaway server on another "
            f"port (e.g. 127.0.0.1:55432)"
        )
    return port


def parse_target(url: str) -> GuardedTarget:
    """Everything that can be refused without opening a connection."""
    what = OPT_IN_ENV
    params = _params(url, what)
    host = str(params.get("host") or params.get("hostaddr") or "").strip()
    if not host:
        raise GuardRefusal(f"{what} names no host; name it explicitly")
    port = _port_of(params, what)
    dbname = str(params.get("dbname") or "")
    if not TEST_DB_RE.match(dbname):
        raise GuardRefusal(
            f"{what} names database {dbname!r} on {host}:{port}, which is not a "
            f"test database (the name must match {TEST_DB_RE.pattern})"
        )
    return GuardedTarget(url=url, host=host, port=port, dbname=dbname)


def opt_in_value(environ: Mapping[str, str]) -> str | None:
    value = (environ.get(OPT_IN_ENV) or "").strip()
    return value or None


def check_opt_in(
    environ: Mapping[str, str], connect: Callable | None = None
) -> GuardedTarget | None:
    """None when the opt-in is absent (the caller skips; nothing connects).

    Otherwise the verified target, or GuardRefusal. The single statement sent
    is `SELECT pg_catalog.current_database()` (schema-qualified, so a search_path
    cannot shadow it), on a bounded autocommit connection that is closed again."""
    url = opt_in_value(environ)
    if url is None:
        return None
    target = parse_target(url)
    where = f"{target.host}:{target.port}/{target.dbname}"
    connect = connect or psycopg.connect  # resolved late: the gate may wrap it
    try:
        with connect(
            url,
            connect_timeout=CHECK_TIMEOUT_SECONDS,
            autocommit=True,
            options=f"-c statement_timeout={CHECK_TIMEOUT_SECONDS * 1000}",
        ) as conn:
            row = conn.execute("SELECT pg_catalog.current_database()").fetchone()
    except GuardRefusal:
        raise
    except Exception as exc:
        raise GuardRefusal(
            f"{OPT_IN_ENV} is set but {where} cannot be reached "
            f"({type(exc).__name__}); opted in means opted in, so this is an "
            "error, not a skip"
        ) from None
    actual = row[0] if row else None
    if not isinstance(actual, str) or not TEST_DB_RE.match(actual):
        raise GuardRefusal(
            f"{OPT_IN_ENV} points at {target.host}:{target.port}, where "
            f"current_database() is {actual!r}, not a test database (the name "
            f"must match {TEST_DB_RE.pattern}); nothing was written"
        )
    if actual != target.dbname:
        raise GuardRefusal(
            f"{OPT_IN_ENV} names database {target.dbname!r} but the server at "
            f"{target.host}:{target.port} reports {actual!r}; a proxy is routing "
            "somewhere the URL does not say"
        )
    return target


def describe(target: GuardedTarget) -> str:
    return (
        f"[mdp-test-db-guard] about to use database {target.dbname!r} at "
        f"{target.host}:{target.port} (from {OPT_IN_ENV}; DATABASE_URL and "
        f"the localhost:{LIVE_PORT} default are not consulted)"
    )


def announce(config, line: str) -> None:
    """Print through the terminal reporter: pytest captures plain prints."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print(line, file=sys.stderr)
        return
    reporter.ensure_newline()
    reporter.write_line(line)


def check_conninfo(conninfo: str = "", **kwargs) -> None:
    """The connect-time gate: an explicit single port, and not 5432."""
    params = _params(conninfo, "a connection string") if conninfo else {}
    for key in ("host", "hostaddr", "port", "dbname", "service"):
        if kwargs.get(key) is not None:
            params[key] = str(kwargs[key])
    _port_of(params, "a test connection")


def install_connection_gate(monkeypatch) -> None:
    """Check the port of every psycopg connection before it is made."""
    from storage import db

    real_connect = psycopg.connect
    real_class_connect = psycopg.Connection.connect
    real_pool = db.ConnectionPool

    def gated_connect(conninfo="", *args, **kwargs):
        check_conninfo(conninfo, **kwargs)
        return real_connect(conninfo, *args, **kwargs)

    def gated_class_connect(conninfo="", *args, **kwargs):
        check_conninfo(conninfo, **kwargs)
        return real_class_connect(conninfo, *args, **kwargs)

    def gated_pool(conninfo="", *args, **kwargs):
        # Pool workers swallow connect errors and retry for 30s; refusing at
        # construction fails the test at once and says why.
        check_conninfo(conninfo, **(kwargs.get("kwargs") or {}))
        return real_pool(conninfo, *args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", gated_connect)
    monkeypatch.setattr(
        psycopg.Connection, "connect", staticmethod(gated_class_connect)
    )
    monkeypatch.setattr(db, "ConnectionPool", gated_pool)


def pin_default_url(monkeypatch, url: str) -> None:
    """A pool built with DATABASE_URL unset can no longer fall back to 5432."""
    from storage import db

    monkeypatch.setattr(db, "_DEFAULT_DATABASE_URL", url)


def refuse_all_connections(monkeypatch) -> list:
    """For tests that must stay offline. Returns the list of attempts, so a
    test whose code under test swallows the error can still be failed."""
    from storage import db

    attempts: list = []

    def refuse(*args, **kwargs):
        attempts.append(args[:1])
        raise RuntimeError(
            "this test is marked no_database: it opens no database connection"
        )

    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "ConnectionPool", refuse)
    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(psycopg.Connection, "connect", staticmethod(refuse))
    return attempts
