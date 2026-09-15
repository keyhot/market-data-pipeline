# tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def postgres_writes_off_by_default(monkeypatch):
    """POSTGRES_WRITE_ENABLED defaults on in production; unit/API tests run
    offline, so force it off. Tests that exercise the write path re-enable it
    with monkeypatch.setenv.

    DATABASE_URL goes with it (KI-079): with writes off, postgres_readable()
    deliberately falls through to DATABASE_URL and pings it, so a developer box
    that exports one makes real connections from the offline suite — 7.22s
    against 1.29s for tests/api/test_health_endpoint.py, measured. The fixture
    owns every variable the offline path reads, not one of the two."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
