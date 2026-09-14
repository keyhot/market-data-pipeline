"""The offline-test contract (KI-079).

`tests/conftest.py` promises that unit and API tests run without a live
Postgres.  Writes-off alone does not deliver that: with `POSTGRES_WRITE_ENABLED=0`
`storage.writes.postgres_readable()` falls through to the real `DATABASE_URL`
and pings it, so `/health`'s tests took 7.22s instead of 1.29s on any box that
exports one.  The fixture has to own *both* variables the offline path reads.
"""

import importlib.util
import os
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _offline_fixture_body():
    """The autouse fixture's undecorated function, so it can be exercised."""
    spec = importlib.util.spec_from_file_location("_conftest_under_test", _CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.postgres_writes_off_by_default
    return fixture._get_wrapped_function()


def test_the_offline_fixture_unsets_database_url():
    body = _offline_fixture_body()
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("DATABASE_URL", "postgresql://unroutable.invalid:5432/nope")
    try:
        body(monkeypatch)
        assert "DATABASE_URL" not in os.environ
    finally:
        monkeypatch.undo()


def test_the_offline_fixture_forces_writes_off():
    body = _offline_fixture_body()
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    try:
        body(monkeypatch)
        assert os.environ["POSTGRES_WRITE_ENABLED"] == "0"
    finally:
        monkeypatch.undo()
