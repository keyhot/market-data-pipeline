# tests/conftest.py
import pytest
from data_guard import REPO_DATA_DIR, assert_tree_unchanged, snapshot_tree


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


def _refuse_database(*args, **kwargs):
    raise RuntimeError(
        "the offline test suite opens no database connections (KI-083); "
        "tests that need Postgres live in tests/integration"
    )


_SPOOLS = {
    "STREAM_EVENT_SPOOL": "stream_events.spool.jsonl",
    "DIRECTOR_EVENT_SPOOL": "director_events.spool.jsonl",
    "BROADCAST_EVENT_SPOOL": "broadcast_events.spool.jsonl",
    # Not spools, but the same hazard: state files the production alerter and
    # broadcast manager read from the checkout they run in.
    "BROADCAST_STATUS_PATH": "broadcast_status.json",
    "OPS_ALERT_STATE": "ops_alert_state.json",
}


def _refuse_mail(*args, **kwargs):
    raise RuntimeError(
        "the test suite sends no mail — the streaming box's .env carries real "
        "alert credentials (the KI-083 class); inject a sender instead"
    )


@pytest.fixture(autouse=True)
def no_database_outside_integration(request, monkeypatch, tmp_path):
    """KI-083: unsetting DATABASE_URL never made the suite offline, because
    storage.db falls back to localhost:5432 — on the streaming box, the
    production database. Two watchdog tests dispatch a real `tick`'s actions,
    so every full run appended a `stream_dropped renderer_blank` to the
    append-only world log and the live director said "We're dropping frames"
    on air.

    So connections are refused where they are made — the pool and the role
    probe in storage.db — and the pool singleton is cleared, because collecting
    tests/integration builds it against the real URL before any fixture runs.
    Callers already degrade on a refused connection: `ping()` reads False,
    event writers spool. The spools move to tmp, because the production
    services flush `data/*.spool.jsonl` from the checkout they run in, and a
    refused write spooled there would reach production one restart later.

    tests/integration is exempt: it is the suite that talks to Postgres, and
    it skips itself when there is none."""
    import smtplib

    # Before the integration exemption: no suite sends mail.
    monkeypatch.setattr(smtplib, "SMTP", _refuse_mail)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _refuse_mail)
    if "integration" in request.node.path.parts:
        return
    import storage.db as db

    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "ConnectionPool", _refuse_database)
    monkeypatch.setattr(db.psycopg, "connect", _refuse_database)
    for var, name in _SPOOLS.items():
        monkeypatch.setenv(var, str(tmp_path / name))


_DATA_BEFORE = pytest.StashKey()


def pytest_sessionstart(session):
    """Snapshot the checkout's data/ before collection, so a write at import time
    or in a module-level statement (collection runs before any fixture) is
    compared too. A fixture alone would take its snapshot after collection."""
    session.config.stash[_DATA_BEFORE] = snapshot_tree(REPO_DATA_DIR)


@pytest.fixture(scope="session", autouse=True)
def repo_data_dir_untouched(request):
    """Fail the run if any test created, modified or deleted anything under the
    checkout's data/ (Sprint 19 draft KI "tests write into data/raw"): three
    API tests wrote fake CSV snapshots into the user's real data/raw, and
    save_csv's retention pruning could delete real ones with the same prefix.
    The snapshot is taken at session start and compared after the last test, so
    a data/ that does not exist yet is fine (the leak is then the directory).
    The redirect below is what keeps this green; this is what makes the next
    leak -- a module-scoped fixture, a subprocess, a new writer -- loud."""
    stash = request.config.stash
    before = stash.get(_DATA_BEFORE, None)
    if before is None:  # sessionstart did not reach this conftest
        before = stash[_DATA_BEFORE] = snapshot_tree(REPO_DATA_DIR)
    yield
    assert_tree_unchanged(REPO_DATA_DIR, before)


@pytest.fixture(autouse=True)
def data_root_in_tmp(monkeypatch, tmp_path):
    """Every writer that resolves a path through storage.naming (raw_data_path,
    raw_event_path, raw_news_path: the ticker, events and news endpoints, the
    scheduler jobs, the news store) writes under a per-test tmp dir, never the
    checkout's data/. csv_write_enabled() still defaults on, so the endpoint
    tests exercise the real save_csv, including its pruning, in the tmp dir.
    The path functions read the module global at call time, so one patch covers
    all three. Not exempted for tests/integration: nothing there needs the
    checkout's data/ either."""
    import storage.naming as naming

    monkeypatch.setattr(naming, "_DATA_ROOT", tmp_path / "data")
