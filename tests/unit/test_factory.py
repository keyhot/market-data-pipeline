import pytest

from ingestion import factory
from ingestion.caching_provider import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_TTL_SECONDS,
    CachingProvider,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    factory.reset_default_provider()
    yield
    factory.reset_default_provider()


def test_defaults_when_env_absent(monkeypatch):
    monkeypatch.delenv(factory.CACHE_TTL_ENV, raising=False)
    monkeypatch.delenv(factory.CACHE_MAX_ENTRIES_ENV, raising=False)

    provider = factory.get_default_provider()
    assert isinstance(provider, CachingProvider)
    assert provider._ttl == DEFAULT_TTL_SECONDS
    assert provider._max == DEFAULT_MAX_ENTRIES


def test_env_overrides_apply(monkeypatch):
    monkeypatch.setenv(factory.CACHE_TTL_ENV, "15")
    monkeypatch.setenv(factory.CACHE_MAX_ENTRIES_ENV, "8")

    provider = factory.get_default_provider()
    assert provider._ttl == 15
    assert provider._max == 8


def test_invalid_env_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv(factory.CACHE_TTL_ENV, "not-a-number")
    monkeypatch.setenv(factory.CACHE_MAX_ENTRIES_ENV, "-3")

    # init_logging in api.main may have set disable_existing_loggers=True
    # earlier in the test run, so re-enable the factory logger explicitly
    # before asserting on warning content.
    factory._logger.disabled = False
    factory._logger.propagate = True
    caplog.set_level("WARNING", logger="ingestion.factory")

    provider = factory.get_default_provider()

    assert provider._ttl == DEFAULT_TTL_SECONDS
    assert provider._max == DEFAULT_MAX_ENTRIES
    messages = [r.getMessage() for r in caplog.records]
    assert any("CACHE_TTL_SECONDS" in m for m in messages)
    assert any("CACHE_MAX_ENTRIES" in m for m in messages)


def test_singleton_caches_first_construction(monkeypatch):
    monkeypatch.setenv(factory.CACHE_TTL_ENV, "10")
    first = factory.get_default_provider()
    # Changing env after first call should NOT affect the existing singleton
    monkeypatch.setenv(factory.CACHE_TTL_ENV, "999")
    second = factory.get_default_provider()
    assert first is second
    assert second._ttl == 10
