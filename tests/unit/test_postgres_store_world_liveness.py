"""storage.postgres_store.world_liveness_times, DB-free.

No live Postgres runs in this suite (tests/integration/test_postgres_store.py
covers the real thing when one is up) — these pin the reader's SQL contract
and its `(signal, event)` return order against a fake pool instead, so the
one piece of new logic this ticket adds isn't exercised by nothing.
"""

from datetime import datetime, timezone

from storage import postgres_store

_SIGNAL_TS = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
_EVENT_TS = datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, rows):
        self._rows = list(rows)
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        return _FakeCursor(self._rows.pop(0))


class _FakeConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc_info):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.timeouts_seen: list[float | None] = []

    def connection(self, timeout=None):
        self.timeouts_seen.append(timeout)
        return _FakeConnCtx(self._conn)


def _wire(monkeypatch, rows):
    conn = _FakeConnection(rows)
    pool = _FakePool(conn)
    monkeypatch.setattr(postgres_store, "get_pool", lambda: pool)
    return conn, pool


def test_returns_signal_then_event_in_that_order(monkeypatch):
    # A swapped return leaves `stale` correct (world.liveness ORs the two
    # ages, so either being old trips it regardless of which is which) but
    # transposes signal_age_s and event_age_s — the two numbers a human
    # reads to tell "the model is dead" from "the world is merely quiet".
    # Nothing else in this ticket's tests would catch that swap.
    _wire(monkeypatch, [(_SIGNAL_TS,), (_EVENT_TS,)])

    latest_signal, latest_event = postgres_store.world_liveness_times()

    assert latest_signal == _SIGNAL_TS
    assert latest_event == _EVENT_TS


def test_excludes_stream_lifecycle_events(monkeypatch):
    conn, _ = _wire(monkeypatch, [(_SIGNAL_TS,), (_EVENT_TS,)])

    postgres_store.world_liveness_times()

    assert len(conn.queries) == 2
    signal_query, event_query = conn.queries
    assert "signals" in signal_query
    assert "world_events" in event_query
    assert "NOT LIKE" in event_query and "stream" in event_query


def test_bounds_the_connection_acquire_wait(monkeypatch):
    # /health cannot pay the pool's 30s default connect-acquire timeout when
    # Postgres is down — see TestRoleProbeLatency in
    # tests/unit/test_db_deploy_guard.py for the 5s budget this protects
    # (KI-024). The caller in api/main.py also gates this behind a
    # confirmed-connected postgres_status(), so this bound is belt-and-braces
    # for the gap between that check and this call, not the only guard.
    _, pool = _wire(monkeypatch, [(_SIGNAL_TS,), (_EVENT_TS,)])

    postgres_store.world_liveness_times()

    assert pool.timeouts_seen == [postgres_store._HEALTH_PROBE_TIMEOUT_SECONDS]


def test_a_never_stored_signal_or_event_reads_as_none(monkeypatch):
    _wire(monkeypatch, [(None,), (None,)])

    latest_signal, latest_event = postgres_store.world_liveness_times()

    assert latest_signal is None
    assert latest_event is None
