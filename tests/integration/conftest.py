"""KI-106: integration tests run only against a throwaway database.

Default-deny: every test in this directory is skipped unless
MDP_TEST_DATABASE_URL is set (no fallback to DATABASE_URL or localhost:5432),
and a set value must pass tests/db_guard.py before anything is sent: an
explicit port other than 5432, a mdp_test_* name in the URL, and
current_database() reporting a mdp_test_* name. A failed check aborts the run
with the reason; it never skips.

Modules that need no database mark themselves `no_database`; they run in every
default run under a refusal of all connections, and a connection attempt
(even one the code under test swallows) fails the test.
"""

import os
from pathlib import Path

import db_guard
import pytest

_HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_database: needs no database; runs with every connection refused, "
        "and fails if anything tries to open one",
    )


def _ours(item):
    return _HERE in Path(item.path).resolve().parents


def pytest_collection_modifyitems(config, items):
    url = db_guard.opt_in_value(os.environ)
    if url is not None:
        try:
            db_guard.parse_target(url)  # no connection: fail before any test
        except db_guard.GuardRefusal as refusal:
            pytest.exit(f"KI-106 test-database guard: {refusal}", returncode=4)
        return
    skip = pytest.mark.skip(reason=db_guard.SKIP_REASON)
    for item in items:
        if _ours(item) and item.get_closest_marker("no_database") is None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def guarded_test_database(request):
    """The verified target. Also installs the connect-time port gate for the
    session, so a pool that somehow falls back to 5432 is refused, not made."""
    patch = pytest.MonkeyPatch()
    try:
        db_guard.install_connection_gate(patch)
        target = db_guard.check_opt_in(os.environ)
    except db_guard.GuardRefusal as refusal:
        patch.undo()
        pytest.exit(f"KI-106 test-database guard: {refusal}", returncode=4)
    if target is None:
        patch.undo()
        pytest.skip(db_guard.SKIP_REASON)
    db_guard.announce(request.config, db_guard.describe(target))
    yield target
    patch.undo()


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
