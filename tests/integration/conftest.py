"""KI-106: integration tests run only against a throwaway database.

Default-deny: every test in this directory is skipped unless
MDP_TEST_DATABASE_URL is set (no fallback to DATABASE_URL or localhost:5432).
A set value must pass tests/db_guard.py before anything is sent: one host, an
explicit port other than 5432 and a mdp_test_* name in the URL (checked without
a connection), then current_database() reporting that same mdp_test_* name.
That second check runs once, when collection finishes and before the first test
of the run; its result is printed, and a failure aborts the run (exit 4) with
the reason. It never skips. `--collect-only` never connects.

Modules that need no database mark themselves `no_database`; they run in every
default run under a refusal of all connections, and a connection attempt (even
one the code under test swallows) fails the test.
"""

import os
from pathlib import Path

import db_guard
import pytest

_HERE = Path(__file__).resolve().parent
_verified: dict = {"target": None, "gate": None}

# Imports of the modules below this directory happen after this file loads and
# before collection finishes: no connection may be opened in between, whatever
# a module does at import. (tests/unit pins the usual suspects statically; this
# is the dynamic net.)
_collection_patch = pytest.MonkeyPatch()
_collection_attempts = db_guard.refuse_all_connections(_collection_patch)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_database: needs no database; runs with every connection refused, "
        "and fails if anything tries to open one",
    )


def _ours(item):
    return _HERE in Path(item.path).resolve().parents


def _needs_database(item):
    return _ours(item) and item.get_closest_marker("no_database") is None


def pytest_collection_modifyitems(config, items):
    needing = [item for item in items if _needs_database(item)]
    if not needing:
        return
    url = db_guard.opt_in_value(os.environ)
    if url is not None:
        try:
            db_guard.parse_target(url)  # no connection: fail before any test
        except db_guard.GuardRefusal as refusal:
            pytest.exit(f"KI-106 test-database guard: {refusal}", returncode=4)
        return
    skip = pytest.mark.skip(reason=db_guard.SKIP_REASON)
    for item in needing:
        item.add_marker(skip)


def pytest_collection_finish(session):
    _collection_patch.undo()
    if _collection_attempts:
        pytest.exit(
            "KI-106 test-database guard: a module tried to open a database "
            f"connection ({len(_collection_attempts)}x) while pytest was "
            "collecting; connections belong inside tests, behind the opt-in",
            returncode=4,
        )
    if (
        session.config.option.collectonly
        or db_guard.opt_in_value(os.environ) is None
        or not any(_needs_database(item) for item in session.items)
    ):
        return
    # Before the first test of the run: install the connect-time port gate for
    # the session, then confirm what the server calls this database.
    gate = pytest.MonkeyPatch()
    _verified["gate"] = gate
    db_guard.scrub_routing_env(gate)
    try:
        db_guard.install_connection_gate(gate)
        target = db_guard.check_opt_in(os.environ)
    except db_guard.GuardRefusal as refusal:
        pytest.exit(f"KI-106 test-database guard: {refusal}", returncode=4)
    _verified["target"] = target
    db_guard.announce(session.config, db_guard.describe(target))


def pytest_sessionfinish(session):
    if _verified["gate"] is not None:
        _verified["gate"].undo()


@pytest.fixture(scope="session")
def guarded_test_database():
    """The target verified when collection finished."""
    if _verified["target"] is None:
        pytest.skip(db_guard.SKIP_REASON)
    return _verified["target"]


@pytest.fixture(autouse=True)
def integration_database(
    request,
    monkeypatch,
    # Named so that this runs after the outer fixtures: they delete
    # DATABASE_URL, and this puts the test URL back.
    postgres_writes_off_by_default,
    no_database_outside_integration,
):
    from storage import db

    if request.node.get_closest_marker("no_database"):
        attempts = db_guard.refuse_all_connections(monkeypatch)
        yield None
        if attempts:
            pytest.fail(
                f"a test marked no_database tried to open a database connection "
                f"({len(attempts)}x); patch the storage reads it makes",
                pytrace=False,
            )
        return
    target = request.getfixturevalue("guarded_test_database")
    monkeypatch.setenv("DATABASE_URL", target.url)
    db_guard.pin_default_url(monkeypatch, target.url)
    monkeypatch.setattr(db, "_pool", None)  # a fresh pool, built on the test URL
    yield target
    if db._pool is not None:
        db._pool.close()
