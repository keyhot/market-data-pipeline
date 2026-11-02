"""F10 (audit 5): a recorder restart must not double count Kraken trades.

`kraken_trade_minutes` is written by an ADDITIVE upsert, and the uids that
keep a trade from being folded twice used to live only in the recorder's
memory. A restarted process therefore folded its whole first window again,
and every trade the old process had already flushed was counted twice.

Nothing here opens a database or a socket. The recorder is real, the store
function it writes through (`storage.liquidation_store`) is real, and the
pool under it is `FakePg`, which EXECUTES the SQL text the store sends
(additive merge, LEAST/GREATEST, DO NOTHING, a SAVEPOINT, a rollback when
the transaction raises) instead of re-implementing what it expects the SQL
to do. A statement the fake does not understand fails the test, so a change
to the SQL has to be mirrored here on purpose.

The tape: trade `i` prints `i * 60 ms` after 00:59:57 UTC, so 100 trades take
6 s, minute 00:59 holds i < 50 and minute 01:00 holds i >= 50, and trade 70 is
a liquidation. A "window" is what the history endpoint returns: the newest
100 trades, newest first.
"""

from __future__ import annotations

import asyncio
import copy
import operator
import re
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.errors import UndefinedTable

import storage.db as storage_db
import storage.liquidation_store as store_mod
from ingestion.liquidation_recorder import LiquidationRecorder
from ingestion.liquidations import aggregate_kraken_minutes, parse_kraken_history

SYM = "PF_XBTUSD"
ETH = "PF_ETHUSD"
START = datetime(2026, 11, 2, 0, 59, 57, tzinfo=timezone.utc)
STEP = timedelta(microseconds=60_000)
LIQ_AT = 70
M0 = datetime(2026, 11, 2, 0, 59, tzinfo=timezone.utc)  # trades 0..49
M1 = M0 + timedelta(minutes=1)  # trades 50..


# --- the store under the recorder ---------------------------------------------


class _Cursor:
    def __init__(self, pg):
        self.pg = pg
        self.rows = []

    def execute(self, sql, params=()):
        self.rows = self.pg.run(sql, params)

    def executemany(self, sql, seq):
        for params in seq:
            self.pg.run(sql, params)

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, pg):
        self.pg = pg

    @contextmanager
    def cursor(self):
        yield _Cursor(self.pg)

    @contextmanager
    def transaction(self):
        """psycopg's nested `transaction()`: a SAVEPOINT in an open transaction."""
        saved = self.pg.snapshot()
        try:
            yield
        except BaseException:
            self.pg.restore(saved)
            raise

    def execute(self, sql, params=()):
        cur = _Cursor(self.pg)
        cur.execute(sql, params)
        return cur


class FakePg:
    def __init__(self):
        self.state_table = True  # False: migrate_019 not applied
        self.down = False
        self.before = None  # hook(sql): may raise, to cut a transaction in half
        self.minutes = {}
        self.coverage_kraken = {}
        self.venue_coverage = {}
        self.liquidations = {}
        self.fold_state = {}

    def _tables(self):
        return (
            self.minutes,
            self.coverage_kraken,
            self.venue_coverage,
            self.liquidations,
            self.fold_state,
        )

    def snapshot(self):
        return copy.deepcopy(self._tables())

    def restore(self, saved):
        for table, kept in zip(self._tables(), saved):
            table.clear()
            table.update(kept)

    @contextmanager
    def connection(self):
        if self.down:
            raise psycopg.OperationalError("connection refused")
        saved = self.snapshot()
        try:
            yield _Conn(self)
        except BaseException:
            self.restore(saved)  # the pool's rollback
            raise

    def run(self, sql, params):
        if self.before:
            self.before(sql)
        text = " ".join(sql.split())
        params = [getattr(p, "obj", p) for p in params]  # unwrap Jsonb
        if "kraken_fold_state" in text:
            if not self.state_table:
                raise UndefinedTable('relation "kraken_fold_state" does not exist')
            return self._fold_state(text, params)
        if text.startswith("INSERT INTO kraken_trade_minutes"):
            return self._minutes(text, params)
        do_nothing = re.fullmatch(
            r"INSERT INTO (\w+) \((.*?)\) VALUES \(.*?\) "
            r"ON CONFLICT (?:\(.*?\) )?DO NOTHING",
            text,
        )
        assert do_nothing, f"the fake does not know this statement: {text}"
        table, columns = do_nothing.group(1), do_nothing.group(2).split(", ")
        row = dict(zip(columns, params))
        if table == "liquidations":
            key = (
                (row["venue"], row["venue_trade_id"])
                if row["venue_trade_id"] is not None
                else tuple(row[c] for c in ("venue", "symbol", "event_ts", "side"))
                + (row["price"], row["size"])
            )
            target = self.liquidations
        elif table == "liquidation_coverage_kraken":
            key, target = (row["symbol"], row["minute"]), self.coverage_kraken
        elif table == "liquidation_coverage":
            key, target = (row["venue"], row["minute"]), self.venue_coverage
        else:
            raise AssertionError(f"the fake has no table {table}")
        target.setdefault(key, row)  # DO NOTHING: the first copy stays
        return []

    def _minutes(self, text, params):
        parsed = re.fullmatch(
            r"INSERT INTO kraken_trade_minutes \((.*?)\) VALUES \(.*?\) "
            r"ON CONFLICT \(symbol, minute\) DO UPDATE SET (.*)",
            text,
        )
        assert parsed, f"the fake does not know this statement: {text}"
        columns = parsed.group(1).split(", ")
        merge = {}
        for m in re.finditer(
            r"(\w+) = (?:(LEAST|GREATEST)\(kraken_trade_minutes\.(\w+), "
            r"EXCLUDED\.(\w+)\)|kraken_trade_minutes\.(\w+) \+ EXCLUDED\.(\w+))",
            parsed.group(2),
        ):
            col = m.group(1)
            if m.group(2):
                assert col == m.group(3) == m.group(4)
                merge[col] = min if m.group(2) == "LEAST" else max
            else:
                assert col == m.group(5) == m.group(6)
                merge[col] = operator.add
        assert set(merge) == set(columns) - {"symbol", "minute"}, text
        row = dict(zip(columns, params))
        have = self.minutes.get((row["symbol"], row["minute"]))
        if have is None:
            self.minutes[(row["symbol"], row["minute"])] = row
        else:
            for col, fn in merge.items():
                have[col] = fn(have[col], row[col])
        return []

    def _fold_state(self, text, params):
        if text.startswith("SELECT symbol, uids FROM kraken_fold_state"):
            return [(s, list(u)) for s, u in self.fold_state.items()]
        assert re.fullmatch(
            r"INSERT INTO kraken_fold_state \(symbol, uids, updated_at\) "
            r"VALUES \(%s, %s, now\(\)\) ON CONFLICT \(symbol\) DO UPDATE SET "
            r"uids = EXCLUDED.uids, updated_at = EXCLUDED.updated_at",
            text,
        ), f"the fake does not know this statement: {text}"
        self.fold_state[params[0]] = list(params[1])  # replace, not merge
        return []


@pytest.fixture(autouse=True)
def pg(monkeypatch):
    fake = FakePg()
    monkeypatch.setattr(store_mod, "get_pool", lambda: fake)

    def refuse(*args, **kwargs):
        raise AssertionError("a real database or socket was requested")

    monkeypatch.setattr(storage_db, "get_pool", refuse)
    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return fake


# --- the tape and the processes -------------------------------------------------


def _ts(i):
    return START + STEP * i


def _row(i, symbol=SYM):
    return {
        "time": _ts(i).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "uid": f"{symbol}:{i}",
        "price": 100.0 + (i % 7),
        "size": 1.0 + (i % 3),
        "side": "buy",
        "type": "liquidation" if i == LIQ_AT else "fill",
    }


def _window(lo, hi, symbol=SYM):
    """What the endpoint returns when trades lo..hi-1 are the newest."""
    return {"history": [_row(i, symbol) for i in reversed(range(lo, hi))]}


def _expected(indices, symbol=SYM):
    """The truth: (count, total size, liquidations) per minute for these trades."""
    out = {}
    for i in indices:
        minute = _ts(i).replace(second=0, microsecond=0)
        count, size, liqs = out.get((symbol, minute), (0, 0.0, 0))
        out[(symbol, minute)] = (
            count + 1,
            size + _row(i)["size"],
            liqs + (i == LIQ_AT),
        )
    return out


def _stored(pg, symbol=SYM):
    return {
        key: (r["trade_count"], r["total_size"], r["liq_count"])
        for key, r in pg.minutes.items()
        if key[0] == symbol
    }


def _count(stored):
    return sum(count for count, _, _ in stored.values())


def _boot(symbols=(SYM,)):
    """A fresh recorder process, wired the way scripts/liquidation_recorder.py does."""
    return LiquidationRecorder(
        store_mod.write_liquidation_batch,
        clock=lambda: 1_790_000_000.0,
        kraken_symbols=symbols,
    )


def _poll(recorder, lo, hi, symbol=SYM):
    recorder.process_kraken_response(symbol, _window(lo, hi, symbol))


def _flush(recorder):
    return asyncio.run(recorder.flush())


# --- the fake is the real SQL ------------------------------------------------------


def test_the_fake_store_adds_like_the_real_upsert(pg):
    # If this stopped adding, the repro below would pass for the wrong reason.
    aggs = aggregate_kraken_minutes(parse_kraken_history(_window(0, 10), SYM))
    store_mod.write_liquidation_batch([], [], aggs, [])
    store_mod.write_liquidation_batch([], [], aggs, [])
    assert _count(_stored(pg)) == 20
    assert (SYM, M0) in pg.minutes
    assert pg.minutes[(SYM, M0)]["min_price"] == 100.0  # LEAST/GREATEST, not a sum
    assert pg.minutes[(SYM, M0)]["max_price"] == 106.0


# --- F10: the repro and its controls -----------------------------------------------


def test_a_restart_over_trades_the_old_process_flushed_counts_each_once(pg):
    # Process A polls trades 0..99 and flushes. It is restarted. While it was
    # down trades 100..149 printed, so B's first window is 50..149: trades
    # 50..99 are the overlap B must not fold again. The audit's repro: 200
    # stored against 150 true.
    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a)
    del a

    b = _boot()
    _poll(b, 50, 150)
    assert _flush(b)

    stored = _stored(pg)
    assert _count(stored) == 150  # F10 stored 200
    assert stored == _expected(range(150))
    assert len(pg.liquidations) == 1  # deduped by uid by the DB
    assert stored[(SYM, M1)][2] == 1  # liq_count 1, not 2


def test_the_same_process_still_counts_each_trade_once(pg):
    # The in-memory set always did this; the fix must keep it.
    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a)
    _poll(a, 50, 150)
    assert _flush(a)
    assert _count(_stored(pg)) == 150
    assert _stored(pg) == _expected(range(150))


def test_a_restart_after_a_full_window_of_downtime_has_nothing_to_remove(pg):
    # No overlap: B's window 100..199 shares no trade with A's 0..99. Every
    # trade must still be counted (a skipped first window would lose 100).
    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a)
    del a

    b = _boot()
    _poll(b, 100, 200)
    assert _flush(b)

    assert _count(_stored(pg)) == 200
    assert _stored(pg) == _expected(range(200))
