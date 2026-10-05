import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ingestion.liquidation_recorder import Buffer, LiquidationRecorder, recorder_enabled
from ingestion.liquidations import KrakenContract, parse_binance
from tests.unit.test_liquidations import BINANCE_FRAME, BYBIT_FRAME, KRAKEN_PAYLOAD, OKX_FRAME

T0 = 1_790_650_800  # a whole minute

OKX_SUBSCRIBED = '{"event":"subscribe","arg":{"channel":"liquidation-orders","instType":"SWAP"}}'
BYBIT_SUBSCRIBED = '{"success":true,"ret_msg":"","conn_id":"x","req_id":"","op":"subscribe"}'
BYBIT_PONG = '{"success":true,"ret_msg":"pong","conn_id":"x","req_id":"","op":"ping"}'


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Sink:
    """Stands in for storage.liquidation_store.write_liquidation_batch."""

    def __init__(self, fail=False):
        self.fail = fail
        self.batches = []

    def __call__(self, liquidations, coverage, kraken_minutes=(), kraken_coverage=()):
        if self.fail:
            raise ConnectionError("postgres down")
        self.batches.append(
            (list(liquidations), list(coverage), list(kraken_minutes), list(kraken_coverage))
        )


def _recorder(sink=None, t=T0 - 5):
    clock = Clock(t)
    return LiquidationRecorder(sink or Sink(), clock=clock), clock


def _flush(recorder):
    return asyncio.run(recorder.flush())


def test_recorder_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("LIQUIDATION_RECORDER_ENABLED", raising=False)
    assert recorder_enabled() is False
    monkeypatch.setenv("LIQUIDATION_RECORDER_ENABLED", "1")
    assert recorder_enabled() is True


def test_binance_url_defaults_to_the_market_path(monkeypatch):
    monkeypatch.delenv("LIQ_BINANCE_WS_URL", raising=False)
    recorder, _ = _recorder()
    # The legacy /ws path connected and stayed silent on 2026-09-29.
    assert recorder._binance_url == "wss://fstream.binance.com/market/ws/!forceOrder@arr"


def test_a_minute_lands_in_the_same_batch_as_the_rows_received_in_it():
    sink = Sink()
    recorder, clock = _recorder(sink)
    recorder.on_binance_frame(BINANCE_FRAME)  # T0-5: before the minute
    clock.t = T0 + 20
    recorder.on_binance_frame(BINANCE_FRAME)
    clock.t = T0 + 40
    recorder.on_pong("binance")
    clock.t = T0 + 61
    recorder.on_pong("binance")
    assert _flush(recorder)
    (liquidations, coverage, _km, _kc), = sink.batches
    assert len(liquidations) == 2
    assert coverage == [("binance", T0, 1)]


def test_a_row_arriving_during_a_failed_write_is_kept_with_the_batch():
    # The race the take/restore design exists for: the write runs in a
    # thread while the event loop keeps receiving frames.
    gate = threading.Event()

    class SlowFailingSink(Sink):
        def __call__(self, liquidations, coverage, kraken_minutes=(), kraken_coverage=()):
            gate.wait(5)
            super().__call__(liquidations, coverage, kraken_minutes, kraken_coverage)

    sink = SlowFailingSink(fail=True)
    recorder, clock = _recorder(sink)
    recorder.on_binance_frame(BINANCE_FRAME)
    for t in (T0 + 20, T0 + 40, T0 + 61):
        clock.t = t
        recorder.on_pong("binance")

    async def scenario():
        flush = asyncio.create_task(recorder.flush())
        await asyncio.sleep(0.05)  # the write is now blocked in its thread
        clock.t = T0 + 62
        recorder.on_binance_frame(BINANCE_FRAME.replace("4012", "7"))
        gate.set()
        return await flush

    assert asyncio.run(scenario()) is False
    sink.fail = False
    assert _flush(recorder)
    (liquidations, coverage, _km, _kc), = sink.batches
    assert sorted(x.size for x in liquidations) == [7, 4012]
    assert coverage == [("binance", T0, 0)]
    assert recorder.buffer.rows_written == 2


def test_overflow_drops_oldest_rows_and_withdraws_pending_coverage():
    buffer = Buffer(max_rows=2)
    buffer.cover("binance", [(T0, 3)])
    a = parse_binance(BINANCE_FRAME.replace("4012", "1"))
    b = parse_binance(BINANCE_FRAME.replace("4012", "2"))
    c = parse_binance(BINANCE_FRAME.replace("4012", "3"))
    buffer.add(a + b)
    assert buffer.coverage == [("binance", T0, 3)]
    buffer.add(c)
    assert [x.size for x in buffer.liquidations] == [2, 3]
    # Rows received in T0 may be among the dropped: T0 can't be claimed.
    assert buffer.coverage == []
    assert buffer.rows_dropped == 1


def test_okx_subscription_ack_proves_the_feed_flows_without_a_liquidation():
    sink = Sink()
    recorder, clock = _recorder(sink)
    recorder.on_okx_frame(OKX_SUBSCRIBED)
    for t in (T0 + 10, T0 + 35, T0 + 61):
        clock.t = t
        assert recorder.on_okx_frame("pong") == "pong"
    _flush(recorder)
    assert sink.batches[0][1] == [("okx", T0, 0)]


def test_okx_error_frame_is_not_a_proof():
    recorder, clock = _recorder()
    recorder.on_okx_frame(OKX_SUBSCRIBED)
    clock.t = T0 + 61
    assert recorder.on_okx_frame('{"event":"error","code":"60012"}') == "error"
    assert recorder.trackers["okx"].last_proof == T0 - 5


def test_okx_data_rows_are_buffered_and_counted():
    sink = Sink()
    recorder, clock = _recorder(sink)
    recorder.on_okx_frame(OKX_SUBSCRIBED)
    clock.t = T0 + 20
    recorder.on_okx_frame(OKX_FRAME)
    clock.t = T0 + 45
    recorder.on_okx_frame("pong")
    clock.t = T0 + 61
    recorder.on_okx_frame("pong")
    _flush(recorder)
    liquidations, coverage, _km, _kc = sink.batches[0]
    assert [x.venue for x in liquidations] == ["okx"]
    assert coverage == [("okx", T0, 1)]


def test_bybit_frames_flow_through_their_own_tracker():
    sink = Sink()
    recorder, clock = _recorder(sink)
    recorder.on_bybit_frame(BYBIT_SUBSCRIBED)
    clock.t = T0 + 25
    recorder.on_bybit_frame(BYBIT_FRAME)
    clock.t = T0 + 50
    recorder.on_bybit_frame(BYBIT_PONG)
    clock.t = T0 + 61
    recorder.on_bybit_frame(BYBIT_PONG)
    _flush(recorder)
    liquidations, coverage, _km, _kc = sink.batches[0]
    assert [(x.venue, x.side) for x in liquidations] == [("bybit", "short")]
    assert coverage == [("bybit", T0, 1)]


def test_binance_without_a_liquidation_claims_no_coverage():
    sink = Sink()
    recorder, clock = _recorder(sink)
    for t in (T0 - 5, T0 + 10, T0 + 30, T0 + 50, T0 + 70, T0 + 130):
        clock.t = t
        recorder.on_pong("binance")
    _flush(recorder)
    assert sink.batches == []


# --- Kraken REST polling ------------------------------------------------------


def test_kraken_symbols_default_to_btc_and_eth(monkeypatch):
    monkeypatch.delenv("LIQ_KRAKEN_SYMBOLS", raising=False)
    recorder, _ = _recorder()
    assert recorder._kraken_symbols == ("PF_XBTUSD", "PF_ETHUSD")
    assert recorder.kraken_gaps == {"PF_XBTUSD": 0, "PF_ETHUSD": 0}


def test_kraken_symbols_configurable_via_env(monkeypatch):
    monkeypatch.setenv("LIQ_KRAKEN_SYMBOLS", "PF_SOLUSD, PF_XRPUSD")
    recorder, _ = _recorder()
    assert recorder._kraken_symbols == ("PF_SOLUSD", "PF_XRPUSD")


def test_kraken_poll_seconds_default_and_env(monkeypatch):
    monkeypatch.delenv("LIQ_KRAKEN_POLL_SECONDS", raising=False)
    recorder, _ = _recorder()
    assert recorder._kraken_poll_seconds == 5.0
    monkeypatch.setenv("LIQ_KRAKEN_POLL_SECONDS", "2.5")
    recorder2, _ = _recorder()
    assert recorder2._kraken_poll_seconds == 2.5


def test_process_kraken_response_buffers_only_liquidations_and_advances_last_ts():
    recorder, _ = _recorder()
    liquidation_row = {
        **KRAKEN_PAYLOAD["history"][0],
        "type": "liquidation",
        "uid": "liq-1",
        "time": "2026-10-05T15:49:10.000000Z",
    }
    payload = {"history": KRAKEN_PAYLOAD["history"] + [liquidation_row]}
    new = recorder.process_kraken_response("PF_XBTUSD", payload)
    assert len(new) == len(payload["history"])  # first poll: everything is "new"
    assert [x.venue for x in recorder.buffer.liquidations] == ["kraken"]
    assert recorder.buffer.liquidations[0].venue_trade_id == "liq-1"
    assert recorder._kraken_last_ts["PF_XBTUSD"] == datetime(
        2026, 10, 5, 15, 49, 10, tzinfo=timezone.utc
    )
    assert recorder.kraken_gaps["PF_XBTUSD"] == 0


def test_process_kraken_response_counts_a_gap_when_window_cant_reach_back():
    recorder, _ = _recorder()
    recorder._kraken_last_ts["PF_XBTUSD"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    assert recorder.kraken_gaps["PF_XBTUSD"] == 1
    assert recorder.kraken_gaps["PF_ETHUSD"] == 0  # the other symbol is untouched


def test_process_kraken_response_empty_poll_leaves_state_untouched():
    recorder, _ = _recorder()
    known = datetime(2026, 10, 5, 15, 0, 0, tzinfo=timezone.utc)
    recorder._kraken_last_ts["PF_XBTUSD"] = known
    recorder.process_kraken_response("PF_XBTUSD", {"history": []})
    assert recorder._kraken_last_ts["PF_XBTUSD"] == known
    assert recorder.kraken_gaps["PF_XBTUSD"] == 0


def test_process_kraken_response_second_poll_only_forwards_rows_after_last_ts():
    recorder, _ = _recorder()
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    first_last_ts = recorder._kraken_last_ts["PF_XBTUSD"]
    # The same window polled again: nothing is new, no re-buffering.
    before = len(recorder.buffer.liquidations)
    new = recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    assert new == []
    assert len(recorder.buffer.liquidations) == before
    assert recorder._kraken_last_ts["PF_XBTUSD"] == first_last_ts


# --- K2: all-trade minute aggregate + per-symbol coverage --------------------


def test_process_kraken_response_buffers_the_all_trade_minute_aggregate():
    recorder, _ = _recorder()
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    # The fixture's 8 rows span two minutes (15:48 and 15:49 UTC).
    assert len(recorder.buffer.kraken_minutes) == 2
    assert sum(a.trade_count for a in recorder.buffer.kraken_minutes) == 8


def test_process_kraken_response_does_not_double_count_a_minute_seen_twice():
    # The exact gap the task flags: a minute polled twice (e.g. the window
    # hasn't moved far enough yet) must contribute its trades to the
    # aggregate exactly once, even though process_kraken_response runs
    # twice over the same rows.
    recorder, _ = _recorder()
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    first_total = sum(a.trade_count for a in recorder.buffer.kraken_minutes)
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)  # same window again
    second_total = sum(a.trade_count for a in recorder.buffer.kraken_minutes)
    assert first_total == 8
    assert second_total == 8  # unchanged: no new aggregate rows were added


def test_process_kraken_response_writes_kraken_coverage_once_last_ts_exists():
    recorder, _ = _recorder()
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)  # first poll: no last_ts yet
    assert recorder.buffer.kraken_coverage == []
    later_row = {
        **KRAKEN_PAYLOAD["history"][-1],
        "uid": "later-trade",
        "time": "2026-10-05T15:51:00.000000Z",  # a whole minute (15:50) has now elapsed
    }
    # Include the last-forwarded trade itself so the window still reaches
    # back to last_ts (no gap) — only then does a later trade prove a
    # whole intervening minute.
    recorder.process_kraken_response(
        "PF_XBTUSD", {"history": [KRAKEN_PAYLOAD["history"][-1], later_row]}
    )
    assert recorder.buffer.kraken_coverage  # non-empty: a minute was proven
    assert all(covered for _, _, covered, _ in recorder.buffer.kraken_coverage)


def test_process_kraken_response_marks_gap_minutes_uncovered():
    recorder, _ = _recorder()
    recorder._kraken_last_ts["PF_XBTUSD"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    recorder.process_kraken_response("PF_XBTUSD", KRAKEN_PAYLOAD)
    assert recorder.buffer.kraken_coverage  # the gap stretch was still vouched for...
    assert all(covered is False for _, _, covered, _ in recorder.buffer.kraken_coverage)


def test_process_kraken_response_resolves_base_units_once_contracts_loaded():
    recorder, _ = _recorder()
    recorder.kraken_contracts["PF_XBTUSD"] = KrakenContract(
        contract_size=1.0, kind="flexible_futures"
    )
    liquidation_row = {
        **KRAKEN_PAYLOAD["history"][0],
        "type": "liquidation",
        "uid": "liq-base-units",
        "time": "2026-10-05T15:49:10.000000Z",
    }
    payload = {"history": KRAKEN_PAYLOAD["history"] + [liquidation_row]}
    recorder.process_kraken_response("PF_XBTUSD", payload)
    (liq,) = recorder.buffer.liquidations
    assert liq.size_unit == "base"
    assert liq.qty_base == pytest.approx(liq.size)


def test_kraken_rest_loop_survives_a_request_failure_and_keeps_polling(monkeypatch):
    calls = {"n": 0}

    def fake_get_json(url, timeout=10.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("boom")
        return {"history": []}

    monkeypatch.setattr("ingestion.liquidation_recorder._get_json", fake_get_json)
    recorder, _ = _recorder()
    recorder._kraken_symbols = ("PF_XBTUSD",)
    recorder._kraken_poll_seconds = 0.01

    async def scenario():
        task = asyncio.create_task(recorder._kraken_rest_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert calls["n"] >= 2  # the failed first call did not kill the loop
