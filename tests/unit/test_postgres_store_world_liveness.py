"""storage.postgres_store.world_liveness_times, DB-free.

No live Postgres runs in this suite (tests/integration/test_postgres_store.py
covers the real thing when one is up) — these pin the reader's SQL contract
and its `(signal, event)` return order against a fake pool instead, so the
one piece of new logic this ticket adds isn't exercised by nothing.
"""

from datetime import datetime, timezone

import pytest

from storage import postgres_store
from world.salience import BROADCAST_APPARATUS_EVENT_TYPES, KNOWN_EVENT_TYPES

_SIGNAL_TS = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
_EVENT_TS = datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self._row = row
        self.queries: list[str] = []
        self.params: list[tuple | None] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        self.params.append(params)
        return _FakeCursor(self._row)


class _FakeConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc_info):
        return False


class _FakePool:
    def __init__(self, conn, on_acquire=None):
        self._conn = conn
        self._on_acquire = on_acquire
        self.timeouts_seen: list[float | None] = []

    def connection(self, timeout=None):
        self.timeouts_seen.append(timeout)
        if self._on_acquire:
            self._on_acquire()
        return _FakeConnCtx(self._conn)


class _Clock:
    """A monotonic clock the test moves by hand, so "the acquire took 1.5s"
    is a statement rather than a sleep."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _wire(monkeypatch, row, acquire_takes: float = 0.0):
    # `row` is the one combined SELECT's result, `(signal, event)`. The
    # leading set_config() call returns the same fake row; nothing reads it.
    clock = _Clock()
    monkeypatch.setattr(postgres_store.time, "monotonic", clock)

    def advance():
        clock.now += acquire_takes

    conn = _FakeConnection(row)
    pool = _FakePool(conn, on_acquire=advance)
    monkeypatch.setattr(postgres_store, "get_pool", lambda: pool)
    return conn, pool


def test_returns_signal_then_event_in_that_order(monkeypatch):
    # A swapped return leaves `stale` correct (world.liveness ORs the two
    # ages, so either being old trips it regardless of which is which) but
    # transposes signal_age_s and event_age_s — the two numbers a human
    # reads to tell "the model is dead" from "the world is merely quiet".
    # Nothing else in this ticket's tests would catch that swap.
    _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS))

    latest_signal, latest_event = postgres_store.world_liveness_times()

    assert latest_signal == _SIGNAL_TS
    assert latest_event == _EVENT_TS


def test_sets_a_statement_timeout_before_the_read(monkeypatch):
    # MINOR-2: `signals` had no index this query could use, so without a
    # statement-level bound `max(signal_timestamp)` is an unbounded
    # sequential scan on an endpoint (/health) the stream watchdog polls
    # under a 5s budget (KI-024). It must run before the SELECT, transaction-
    # local (set_config's third argument), or it protects nothing.
    conn, _ = _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS))

    postgres_store.world_liveness_times()

    assert conn.queries[0] == "SELECT set_config('statement_timeout', %s, true)"
    assert len(conn.queries) == 2


def test_one_deadline_covers_the_acquire_and_both_reads(monkeypatch):
    # KI-069: this used to be three independent bounds — a 2s acquire, then a
    # per-statement 2s on each of two SELECTs — so the reader alone could
    # spend 6s of a 5s watchdog budget. Now whatever the acquire spends comes
    # off the statement's allowance, and both maxima are one statement, so
    # one statement_timeout bounds them together.
    conn, pool = _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS), acquire_takes=1.5)

    postgres_store.world_liveness_times(timeout_seconds=2.0)

    assert pool.timeouts_seen == [2.0]
    assert conn.params[0] == ("500ms",)
    assert len([q for q in conn.queries if "max(" in q]) == 1


def test_a_spent_budget_never_disables_the_statement_timeout(monkeypatch):
    # `statement_timeout = 0` means NO timeout in Postgres. A remaining
    # budget that truncates to 0ms must refuse to read, not read unbounded.
    conn, _ = _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS), acquire_takes=1.9996)

    with pytest.raises(TimeoutError):
        postgres_store.world_liveness_times(timeout_seconds=2.0)

    assert conn.queries == []


def test_no_budget_left_does_not_even_acquire(monkeypatch):
    # The caller hands over what is left of /health's budget; when that is
    # nothing, refuse before touching the pool. psycopg_pool would raise
    # PoolTimeout for timeout<=0 on its own, but the reader's contract is
    # one exception type for "the deadline was spent", not two.
    _, pool = _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS))

    with pytest.raises(TimeoutError):
        postgres_store.world_liveness_times(timeout_seconds=0.0)

    assert pool.timeouts_seen == []


def test_excludes_broadcast_apparatus_events(monkeypatch):
    conn, _ = _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS))

    postgres_store.world_liveness_times()

    assert len(conn.queries) == 2
    query = conn.queries[1]
    assert "FROM signals" in query
    assert "FROM world_events" in query
    # MINOR-4: the backslash is what makes '_' a literal underscore rather
    # than a single-character LIKE wildcard — dropping it (e.g. 'stream_%')
    # would additionally exclude any type merely starting "stream" followed
    # by any character, not just the four stream_* lifecycle rows. Assert
    # both escaped prefixes are present, not just "the word stream/broadcast
    # appears somewhere".
    assert "'stream\\_%'" in query
    assert "'broadcast\\_%'" in query
    assert "NOT IN ('scene_switched', 'commentary_spoken')" in query


def _sql_predicate_excludes(event_type: str) -> bool:
    """Hand-translation of world_liveness_times()'s event-query WHERE clause
    into Python, checked against the whole registry below rather than
    assumed correct — the same technique the review used to clear MINOR-4/
    MINOR-9 in the first place. `NOT LIKE 'x\\_%'` means "does not start with
    the literal prefix x_"; the escaped underscore is why `.startswith` is
    the right translation rather than a wildcard match."""
    return (
        event_type.startswith("stream_")
        or event_type.startswith("broadcast_")
        or event_type in ("scene_switched", "commentary_spoken")
    )


def test_broadcast_apparatus_event_types_is_a_subset_of_the_registry():
    # MINOR-5's drift guard, part 1: the exclusion set can never silently
    # name an event type that doesn't exist.
    assert BROADCAST_APPARATUS_EVENT_TYPES <= KNOWN_EVENT_TYPES


def test_the_sql_predicate_excludes_exactly_the_broadcast_apparatus_registry():
    # MINOR-5's drift guard, part 2: the SQL and the named registry agree,
    # over every event type the world can currently emit — not just the four
    # this ticket started with. `trader_*` (real dry-run trading activity,
    # not broadcast plumbing) must stay OUT of the excluded set; if it ever
    # ends up in BROADCAST_APPARATUS_EVENT_TYPES this fails because the SQL
    # predicate doesn't exclude it.
    excluded = {t for t in KNOWN_EVENT_TYPES if _sql_predicate_excludes(t)}
    assert excluded == BROADCAST_APPARATUS_EVENT_TYPES


def test_bounds_the_connection_acquire_wait(monkeypatch):
    # /health cannot pay the pool's 30s default connect-acquire timeout when
    # Postgres is down — see TestRoleProbeLatency in
    # tests/unit/test_db_deploy_guard.py for the 5s budget this protects
    # (KI-024). The caller in api/main.py also gates this behind a
    # confirmed-readable postgres_readable(), so this bound is belt-and-braces
    # for the gap between that check and this call, not the only guard.
    _, pool = _wire(monkeypatch, (_SIGNAL_TS, _EVENT_TS))

    postgres_store.world_liveness_times()

    assert pool.timeouts_seen == [postgres_store._HEALTH_PROBE_TIMEOUT_SECONDS]


def test_a_never_stored_signal_or_event_reads_as_none(monkeypatch):
    _wire(monkeypatch, (None, None))

    latest_signal, latest_event = postgres_store.world_liveness_times()

    assert latest_signal is None
    assert latest_event is None
