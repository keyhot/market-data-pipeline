# tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def postgres_writes_off_by_default(monkeypatch):
    """POSTGRES_WRITE_ENABLED defaults on in production; unit/API tests run
    offline, so force it off. Tests that exercise the write path re-enable it
    with monkeypatch.setenv."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "0")
