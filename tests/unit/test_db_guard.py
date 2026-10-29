"""KI-106: the integration suite can only ever talk to a throwaway database.

`poetry run pytest` used to run ~20 DB tests (17 of them DELETE/INSERT) against
whatever answered on localhost:5432 — on the streaming box, the live database.
The guard in tests/db_guard.py + tests/integration/conftest.py makes that
impossible: integration tests are opt-in through MDP_TEST_DATABASE_URL (no
fallback to DATABASE_URL or the localhost:5432 default), the target must be a
`mdp_test_*` database on a port other than 5432, and every connection the
integration tests open is checked for the port again.

Everything here runs on fakes. Nothing connects to a database, and the one
end-to-end check runs a scratch pytest project in a subprocess whose
connection functions are replaced by a recorder that raises, so even a broken
guard cannot reach a server from this file.
"""

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

import db_guard

TESTS = Path(__file__).resolve().parents[1]
REPO = TESTS.parent
OPT_IN = "MDP_TEST_DATABASE_URL"
LIVE_URL = "postgresql://market_data:market_data@localhost:5432/market_data"
TEST_URL = "postgresql://tester:hunter2@localhost:55432/mdp_test_ki106"


class _FakeConnection:
    def __init__(self, dbname, statements):
        self._dbname = dbname
        self.statements = statements
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self

    def fetchone(self):
        return (self._dbname,)


class _Server:
    """A connect() stand-in that records every call and every statement."""

    def __init__(self, reported_name):
        self.reported_name = reported_name
        self.calls = []
        self.statements = []
        self.connections = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        conn = _FakeConnection(self.reported_name, self.statements)
        self.connections.append(conn)
        return conn


# --- (a) no opt-in: skip, and nothing connects ------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {OPT_IN: ""},
        {OPT_IN: "   "},
        # No fallback to the ordinary settings, however enticing they look.
        {"DATABASE_URL": LIVE_URL},
        {"DATABASE_URL": TEST_URL, "POSTGRES_DISPOSABLE": "1"},
        {"PGHOST": "localhost", "PGPORT": "5432", "PGDATABASE": "market_data"},
    ],
)
def test_without_the_opt_in_nothing_is_selected_and_nothing_connects(environ):
    server = _Server("mdp_test_ki106")

    assert db_guard.check_opt_in(environ, connect=server) is None
    assert server.calls == []


def test_the_skip_reason_names_the_opt_in_variable():
    assert OPT_IN in db_guard.SKIP_REASON
    assert "5432" in db_guard.SKIP_REASON


# --- (b) refusals: before any write, with a clear error ---------------------


@pytest.mark.parametrize(
    "reported",
    ["market_data", "postgres", "mdp_testing", "MDP_TEST_X", "xmdp_test_a", "mdp_test_"],
)
def test_a_database_that_is_not_a_test_database_is_refused_before_any_write(reported):
    server = _Server(reported)

    with pytest.raises(db_guard.GuardRefusal) as refusal:
        db_guard.check_opt_in({OPT_IN: TEST_URL}, connect=server)

    message = str(refusal.value)
    assert OPT_IN in message
    assert repr(reported) in message
    assert "mdp_test_" in message
    # The only thing the guard ever sends is the name query, on a connection
    # it closes again.
    assert server.statements == ["SELECT current_database()"]
    assert all(conn.closed for conn in server.connections)
    assert "hunter2" not in message


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:hunter2@localhost:5432/mdp_test_x",
        "postgresql://u:hunter2@localhost/mdp_test_x",  # libpq would pick 5432
        "postgresql://u:hunter2@localhost:55432,localhost:5432/mdp_test_x",
        "postgresql://u:hunter2@localhost:55432,localhost:55433/mdp_test_x",
        "postgresql://u:hunter2@localhost:abc/mdp_test_x",
        "host=localhost port=5432 dbname=mdp_test_x user=u password=hunter2",
        "host=localhost dbname=mdp_test_x user=u password=hunter2",
        LIVE_URL,
    ],
)
def test_port_5432_or_no_port_is_refused_before_any_connection(url):
    server = _Server("mdp_test_x")

    with pytest.raises(db_guard.GuardRefusal) as refusal:
        db_guard.check_opt_in({OPT_IN: url}, connect=server)

    assert server.calls == []
    assert OPT_IN in str(refusal.value)
    assert "hunter2" not in str(refusal.value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:hunter2@localhost:55432/market_data",
        "postgresql://u:hunter2@localhost:55432/mdp_testing",
        "postgresql://u:hunter2@localhost:55432",
        "host=localhost port=55432 user=u password=hunter2",
    ],
)
def test_a_url_that_does_not_name_a_test_database_is_refused_before_any_connection(
    url,
):
    server = _Server("mdp_test_x")

    with pytest.raises(db_guard.GuardRefusal) as refusal:
        db_guard.check_opt_in({OPT_IN: url}, connect=server)

    assert server.calls == []
    assert "mdp_test_" in str(refusal.value)
    assert "hunter2" not in str(refusal.value)


def test_an_unparseable_value_is_refused_without_echoing_it():
    server = _Server("mdp_test_x")

    with pytest.raises(db_guard.GuardRefusal) as refusal:
        db_guard.check_opt_in({OPT_IN: "hunter2 ==="}, connect=server)

    assert server.calls == []
    assert "hunter2" not in str(refusal.value)


def test_an_unreachable_opt_in_is_an_error_not_a_skip():
    def down(url, **kwargs):
        raise psycopg.OperationalError("connection refused hunter2")

    with pytest.raises(db_guard.GuardRefusal) as refusal:
        db_guard.check_opt_in({OPT_IN: TEST_URL}, connect=down)

    assert "localhost:55432" in str(refusal.value)
    assert "hunter2" not in str(refusal.value)


# --- (c) a test-pattern name passes -----------------------------------------


@pytest.mark.parametrize(
    "url,host,port,dbname",
    [
        (TEST_URL, "localhost", 55432, "mdp_test_ki106"),
        ("postgres://u:p@127.0.0.1:55433/mdp_test_a", "127.0.0.1", 55433, "mdp_test_a"),
        (
            "host=db.internal port=6543 dbname=mdp_test_0_a user=u",
            "db.internal",
            6543,
            "mdp_test_0_a",
        ),
    ],
)
def test_a_test_database_off_port_5432_is_accepted_and_described(
    url, host, port, dbname
):
    server = _Server(dbname)

    target = db_guard.check_opt_in({OPT_IN: url}, connect=server)

    assert (target.url, target.host, target.port, target.dbname) == (
        url,
        host,
        port,
        dbname,
    )
    assert server.statements == ["SELECT current_database()"]
    assert server.connections[0].closed
    # The probe is bounded: a black-holed host must not hang the run.
    assert server.calls[0][1].get("connect_timeout")
    line = db_guard.describe(target)
    assert dbname in line and f"{host}:{port}" in line
    assert "hunter2" not in line and "p@" not in line


# --- the connect-time gate and the refuse-everything mode -------------------


@pytest.mark.parametrize(
    "conninfo,allowed",
    [
        ("postgresql://u:p@localhost:55432/mdp_test_x", True),
        ("host=127.0.0.1 port=55432 dbname=mdp_test_x", True),
        ("postgresql://u:p@localhost:5432/market_data", False),
        ("postgresql://u:p@localhost/market_data", False),
        ("host=localhost dbname=x", False),
        ("postgresql://u:p@a:55432,b:5432/x", False),
        ("", False),
        ("not a conninfo ===", False),
    ],
)
def test_the_gate_lets_only_an_explicit_non_5432_port_through(conninfo, allowed):
    if allowed:
        db_guard.check_conninfo(conninfo)
    else:
        with pytest.raises(db_guard.GuardRefusal):
            db_guard.check_conninfo(conninfo)


def test_the_gate_stops_every_psycopg_entry_point_before_the_real_connect(
    monkeypatch,
):
    reached = []

    def spy(conninfo="", *args, **kwargs):
        reached.append(conninfo)
        return "connection"

    monkeypatch.setattr(psycopg, "connect", spy)
    monkeypatch.setattr(psycopg.Connection, "connect", staticmethod(spy))
    db_guard.install_connection_gate(monkeypatch)

    for entry in (psycopg.connect, psycopg.Connection.connect):
        with pytest.raises(db_guard.GuardRefusal):
            entry(LIVE_URL, connect_timeout=2)
        with pytest.raises(db_guard.GuardRefusal):
            entry(conninfo="host=localhost dbname=x")
        assert entry(TEST_URL) == "connection"

    assert reached == [TEST_URL, TEST_URL]


def test_the_default_url_is_pinned_away_from_5432(monkeypatch):
    from storage import db

    assert "5432" in db._DEFAULT_DATABASE_URL  # what the pin has to replace
    db_guard.pin_default_url(monkeypatch, TEST_URL)

    assert db._DEFAULT_DATABASE_URL == TEST_URL


def test_refuse_all_connections_blocks_and_counts_each_attempt(monkeypatch):
    from storage import db

    attempts = db_guard.refuse_all_connections(monkeypatch)

    assert db._pool is None
    with pytest.raises(RuntimeError):
        psycopg.connect(TEST_URL)
    with pytest.raises(RuntimeError):
        db.ConnectionPool(TEST_URL)
    assert len(attempts) == 2


# --- collection time: no module under tests/integration connects on import --

_CONNECTING = {"ping", "get_pool", "connect", "ConnectionPool"}


class _ImportTimeCalls(ast.NodeVisitor):
    """Calls that run when a module is imported (so, at collection)."""

    def __init__(self):
        self.found = []

    def visit_Call(self, node):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _CONNECTING:
            self.found.append((node.lineno, name))
        self.generic_visit(node)

    def _function(self, node):
        # A def runs nothing at import except its decorators and defaults.
        for expr in node.decorator_list:
            self.visit(expr)
        for expr in [*node.args.defaults, *node.args.kw_defaults]:
            if expr is not None:
                self.visit(expr)

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node):
        pass


def import_time_calls(source):
    visitor = _ImportTimeCalls()
    visitor.visit(ast.parse(source))
    return visitor.found


def test_the_import_time_scan_sees_what_it_is_meant_to_see():
    assert import_time_calls("pytestmark = skipif(not ping(), reason='x')") == [
        (1, "ping")
    ]
    assert import_time_calls("pool = get_pool()") == [(1, "get_pool")]
    assert import_time_calls("@pytest.mark.skipif(not ping())\ndef t(): ...") == [
        (1, "ping")
    ]
    assert import_time_calls("def helper():\n    return get_pool()\n") == []
    assert import_time_calls("f = lambda: ping()") == []


def test_no_integration_module_connects_at_collection():
    modules = sorted((TESTS / "integration").glob("*.py"))
    assert modules, "tests/integration has no modules — the scan is broken"
    offenders = {
        path.name: found
        for path in modules
        if (found := import_time_calls(path.read_text()))
    }
    assert offenders == {}, (
        "these integration modules connect while pytest is collecting, before "
        f"any opt-in is consulted: {offenders}"
    )


# --- end to end: the real integration conftest, on a scratch project --------

_BLOCKER = '''
"""Replaces every way in: nothing in the scratch run may open a connection."""
import os

import psycopg

_LOG = os.environ["MDP_GUARD_ATTEMPT_LOG"]


def _blocked(*args, **kwargs):
    with open(_LOG, "a") as fh:
        fh.write("connect attempted\\n")
    raise RuntimeError("blocked: the guard's end-to-end test allows no connection")


psycopg.connect = _blocked
psycopg.Connection.connect = staticmethod(_blocked)

import storage.db as _db  # noqa: E402

# Second belt: a pool that falls back to its default lands on a closed port.
_db._DEFAULT_DATABASE_URL = "postgresql://nobody@127.0.0.1:1/nobody"
'''

_SAMPLE = '''
import psycopg
import pytest

from storage.db import get_pool


def test_needs_the_database():
    with get_pool().connection() as conn:
        conn.execute("SELECT 1")


@pytest.mark.no_database
def test_offline_and_stays_offline():
    assert True


@pytest.mark.no_database
def test_offline_but_reaches_for_the_database():
    try:
        psycopg.connect("postgresql://u@localhost:55432/mdp_test_x")
    except Exception:
        pass  # swallowed, like api/routers/world.py swallows enrichment errors
'''

_SCRUBBED = ("DATABASE_URL", OPT_IN, "POSTGRES_DISPOSABLE", "DEPLOY_ROLE")


def _scratch_run(tmp_path, opt_in):
    """Run pytest on a copy of the real conftest files plus a sample module.

    Returns (completed process, text of the attempt log)."""
    project = tmp_path / "project"
    integration = project / "tests" / "integration"
    integration.mkdir(parents=True)
    shutil.copy(TESTS / "conftest.py", project / "tests")
    shutil.copy(TESTS / "db_guard.py", project / "tests")
    shutil.copy(TESTS / "integration" / "conftest.py", integration)
    (integration / "test_sample.py").write_text(_SAMPLE)
    (tmp_path / "guard_blocker.py").write_text(_BLOCKER)
    log = tmp_path / "attempts.log"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _SCRUBBED and not key.startswith("PG")
    }
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(REPO)])
    env["MDP_GUARD_ATTEMPT_LOG"] = str(log)
    if opt_in is not None:
        env[OPT_IN] = opt_in
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "guard_blocker",
            "-p", "no:cacheprovider", "-rs", "-q", "tests",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result, (log.read_text() if log.exists() else "")


def test_end_to_end_without_the_opt_in_integration_tests_skip_and_nothing_connects(
    tmp_path,
):
    result, attempts = _scratch_run(tmp_path, opt_in=None)
    output = result.stdout + result.stderr

    assert attempts == "", output
    assert "SKIPPED" in output and OPT_IN in output, output
    assert "1 skipped" in output, output  # test_needs_the_database
    # A test marked no_database may not reach for a database either, even
    # when the code under test swallows the refusal: that one errors in
    # teardown, the one that stays offline does not.
    assert "tried to open a database connection" in output, output
    assert "2 passed" in output and "1 error" in output, output
    assert result.returncode == 1, output


def test_end_to_end_the_opt_in_on_port_5432_aborts_the_run_before_any_connection(
    tmp_path,
):
    result, attempts = _scratch_run(
        tmp_path, opt_in="postgresql://u:hunter2@localhost:5432/mdp_test_x"
    )
    output = result.stdout + result.stderr

    assert attempts == "", output
    assert result.returncode == 4, output
    assert OPT_IN in output and "5432" in output, output
    assert "hunter2" not in output, output
