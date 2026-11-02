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
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.errors import UndefinedTable

import storage.db as storage_db
import storage.liquidation_store as store_mod
from ingestion.liquidation_recorder import KRAKEN_SEEN_UID_CAP, LiquidationRecorder
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
    recorder = LiquidationRecorder(
        store_mod.write_liquidation_batch,
        clock=lambda: 1_790_000_000.0,
        kraken_symbols=symbols,
        load_kraken_seen=store_mod.load_kraken_seen_uids,
    )
    recorder.seed_kraken_state()
    return recorder


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


# === added with the fix =============================================================


def test_a_minute_split_across_the_restart_is_counted_exactly(pg):
    # Minute 01:00 holds trades 50..: A folded 50..99, B folds 100..149. The
    # total is 100: not 150 (the overlap twice) and not 50 (a replace-style
    # store, or skipping B's first window, loses what printed while down).
    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a)
    before = _stored(pg)
    assert before[(SYM, M1)][0] == 50
    del a

    b = _boot()
    _poll(b, 50, 150)
    assert _flush(b)
    after = _stored(pg)

    assert after[(SYM, M0)] == before[(SYM, M0)]  # the finished minute is untouched
    assert after[(SYM, M1)][0] == 100
    assert after == _expected(range(150))  # sizes and liq_count too


def test_trades_folded_but_never_flushed_are_counted_by_the_next_process(pg):
    # A is killed after folding 100..139 and before flushing them. Those
    # trades are not in the DB, so they must not be in the persisted state
    # either: B's window 90..189 has to count 100..189, not skip 100..139.
    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a)
    _poll(a, 40, 140)  # folded into the buffer only
    del a

    b = _boot()
    _poll(b, 90, 190)
    assert _flush(b)

    assert _count(_stored(pg)) == 190
    assert _stored(pg) == _expected(range(190))


def test_a_failed_write_keeps_the_state_with_its_batch(pg):
    # The batch (aggregates AND the state that vouches for them) is restored
    # after a failed write. No poll arrives before the retry, so nothing
    # re-snapshots the state: it has to come back with the batch.
    a = _boot()
    _poll(a, 0, 100)
    pg.down = True
    assert _flush(a) is False
    assert pg.minutes == {} and pg.fold_state == {}
    pg.down = False
    assert _flush(a) is True
    assert _count(_stored(pg)) == 100
    assert set(pg.fold_state[SYM]) == {f"{SYM}:{i}" for i in range(100)}
    del a

    b = _boot()
    _poll(b, 50, 150)
    assert _flush(b)
    assert _stored(pg) == _expected(range(150))


def test_the_state_lands_in_the_same_transaction_as_the_minutes(pg):
    # The connection dies on the state statement, AFTER the minute upserts
    # ran. Both must roll back together; the retry then applies the batch once.
    def cut(sql):
        if "kraken_fold_state" in sql:
            raise psycopg.OperationalError("connection lost")

    a = _boot()
    _poll(a, 0, 100)
    pg.before = cut
    assert _flush(a) is False
    assert pg.minutes == {} and pg.fold_state == {}

    pg.before = None
    assert _flush(a) is True
    assert _stored(pg) == _expected(range(100))  # once, not twice
    assert len(pg.fold_state[SYM]) == 100


def test_a_database_without_the_state_table_still_takes_the_batch(pg):
    # Deploy order: this code may run before migrate_019.sql is applied. The
    # recorder must keep recording (as it did before the fix), once per
    # batch, with the state write skipped inside its SAVEPOINT.
    pg.state_table = False
    assert store_mod.load_kraken_seen_uids() == {}

    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a) is True
    assert _stored(pg) == _expected(range(100))  # not rolled back, not repeated
    assert pg.fold_state == {}

    pg.state_table = True  # the owner applies the migration later
    _poll(a, 50, 150)
    assert _flush(a) is True
    assert _stored(pg) == _expected(range(150))
    assert len(pg.fold_state[SYM]) == 150


def test_a_recorder_without_a_loader_does_not_touch_the_state_table(pg):
    recorder = LiquidationRecorder(
        store_mod.write_liquidation_batch,
        clock=lambda: 1_790_000_000.0,
        kraken_symbols=(SYM,),
    )
    assert recorder._kraken_seeded  # nothing to wait for
    pg.state_table = False
    _poll(recorder, 0, 100)
    assert _flush(recorder) is True
    assert _count(_stored(pg)) == 100


def test_each_symbol_keeps_its_own_state(pg):
    a = _boot((SYM, ETH))
    _poll(a, 0, 100, ETH)
    assert _flush(a)
    _poll(a, 0, 100, SYM)  # a batch that folds BTC only
    assert _flush(a)
    assert set(pg.fold_state) == {SYM, ETH}  # ETH's state survived BTC's batch
    del a

    b = _boot((SYM, ETH))
    _poll(b, 50, 150, ETH)  # overlaps A's ETH window
    _poll(b, 100, 200, SYM)  # does not overlap A's BTC window
    assert _flush(b)
    assert _stored(pg, ETH) == _expected(range(150), ETH)
    assert _stored(pg, SYM) == _expected(range(200), SYM)


def test_the_persisted_set_is_the_newest_uids_up_to_the_cap(pg):
    a = _boot()
    for lo in range(0, 600, 100):
        _poll(a, lo, lo + 100)
    assert _flush(a)
    uids = pg.fold_state[SYM]
    assert len(uids) == KRAKEN_SEEN_UID_CAP
    assert {f"{SYM}:{i}" for i in range(500, 600)} <= set(uids)
    assert f"{SYM}:0" not in uids


def test_polling_waits_for_the_state_and_the_first_window_is_not_recounted(
    pg, monkeypatch
):
    # Through the real REST loop. The first load fails (Postgres not up yet):
    # no history request may go out until the state is loaded, otherwise the
    # first window would be folded without it and then overwrite the state.
    a = _boot()
    _poll(a, 0, 100)
    assert _flush(a)
    del a

    loads = {"n": 0}
    history_after_loads = []

    def flaky_load():
        loads["n"] += 1
        if loads["n"] == 1:
            raise ConnectionError("postgres is not up yet")
        return store_mod.load_kraken_seen_uids()

    def fake_get_json(url, timeout=10.0):
        if "history" not in url:
            return {}
        history_after_loads.append(loads["n"])
        return _window(50, 150)

    monkeypatch.setattr("ingestion.liquidation_recorder._get_json", fake_get_json)
    b = LiquidationRecorder(
        store_mod.write_liquidation_batch,
        clock=lambda: 1_790_000_000.0,
        kraken_symbols=(SYM,),
        kraken_poll_seconds=0.01,
        load_kraken_seen=flaky_load,
    )

    async def scenario():
        task = asyncio.create_task(b._kraken_rest_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await b.flush()

    asyncio.run(scenario())

    assert loads["n"] >= 2
    assert history_after_loads and min(history_after_loads) >= 2
    assert len(history_after_loads) > 1  # the same window, polled again and again
    assert _stored(pg) == _expected(range(150))
