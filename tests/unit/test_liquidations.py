import json
from datetime import datetime, timezone

import pytest

from ingestion.liquidations import (
    LONG,
    SHORT,
    CoverageTracker,
    OkxContract,
    okx_contracts,
    parse_binance,
    parse_okx,
    parse_okx_data,
    parse_bybit,
)

# Real frames, captured 2026-09-29 from the live feeds.
BINANCE_FRAME = (
    '{"e":"forceOrder","E":1790650456242,"o":{"s":"BRUSDT","S":"SELL","o":"LIMIT",'
    '"f":"IOC","q":"4012","p":"0.8375800","ap":"0.8560330","X":"FILLED","l":"81",'
    '"z":"4012","T":1790650455232,"ps":"BRUSDT","st":1}}'
)
OKX_FRAME = (
    '{"arg":{"channel":"liquidation-orders","instType":"SWAP"},"data":[{"details":'
    '[{"bkLoss":"0","bkPx":"14.526","ccy":"","posSide":"short","side":"buy",'
    '"sz":"68","ts":"1790650761954"}],"instFamily":"NMR-USDT","instId":'
    '"NMR-USDT-SWAP","instType":"SWAP","uly":"NMR-USDT"}]}'
)
LINEAR = {"NMR-USDT-SWAP": OkxContract(ct_val=0.1, ct_mult=1, ct_type="linear")}


# --- Binance -----------------------------------------------------------------


def test_binance_sell_liquidates_a_long_at_the_average_fill():
    (liq,) = parse_binance(BINANCE_FRAME)
    assert liq.venue == "binance"
    assert liq.symbol == "BRUSDT"
    assert liq.side == LONG
    # ap (0.856...), not the order's limit p (0.8375...): p is where the
    # engine was willing to go, ap is where it traded.
    assert liq.price == pytest.approx(0.8560330)
    assert liq.size == pytest.approx(4012)
    assert liq.qty_base == pytest.approx(4012)
    assert liq.notional == pytest.approx(4012 * 0.8560330)
    assert liq.event_ts == datetime.fromtimestamp(1790650455.232, tz=timezone.utc)
    assert liq.source == "ws"


def test_binance_buy_liquidates_a_short():
    frame = BINANCE_FRAME.replace('"S":"SELL"', '"S":"BUY"')
    (liq,) = parse_binance(frame)
    assert liq.side == SHORT


def test_binance_uses_filled_not_original_quantity():
    frame = BINANCE_FRAME.replace('"z":"4012"', '"z":"1000"')
    (liq,) = parse_binance(frame)
    assert liq.size == pytest.approx(1000)


def test_binance_combined_stream_envelope_is_unwrapped():
    wrapped = json.dumps({"stream": "!forceOrder@arr", "data": json.loads(BINANCE_FRAME)})
    assert len(parse_binance(wrapped)) == 1


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        "[]",
        '{"e":"kline"}',
        BINANCE_FRAME.replace('"z":"4012"', '"z":"0"'),
        BINANCE_FRAME.replace('"S":"SELL"', '"S":"HOLD"'),
        BINANCE_FRAME.replace('"ap":"0.8560330",', ""),
    ],
)
def test_binance_non_liquidations_yield_nothing(frame):
    assert parse_binance(frame) == []


# --- OKX ---------------------------------------------------------------------


def test_okx_buy_liquidates_a_short_and_converts_contracts():
    kind, (liq,) = parse_okx(OKX_FRAME, LINEAR)
    assert kind == "data"
    assert liq.venue == "okx"
    assert liq.symbol == "NMR-USDT-SWAP"
    assert liq.side == SHORT
    assert liq.size == pytest.approx(68)
    assert liq.size_unit == "contracts"
    assert liq.qty_base == pytest.approx(6.8)  # 68 contracts × 0.1 NMR
    assert liq.notional == pytest.approx(6.8 * 14.526)
    assert liq.raw["detail"]["posSide"] == "short"


def test_okx_side_not_possible_decides_the_liquidated_side():
    # One-way mode reports posSide "net"; the order side still decides it.
    frame = OKX_FRAME.replace('"posSide":"short","side":"buy"', '"posSide":"net","side":"sell"')
    _, (liq,) = parse_okx(frame, LINEAR)
    assert liq.side == LONG


def test_okx_inverse_contract_value_is_quote():
    inverse = {"NMR-USDT-SWAP": OkxContract(ct_val=100, ct_mult=1, ct_type="inverse")}
    _, (liq,) = parse_okx(OKX_FRAME, inverse)
    assert liq.notional == pytest.approx(6800)
    assert liq.qty_base == pytest.approx(6800 / 14.526)


def test_okx_unknown_contract_keeps_the_row_without_quantities():
    _, (liq,) = parse_okx(OKX_FRAME, {})
    assert liq.qty_base is None and liq.notional is None
    assert liq.size == pytest.approx(68)


@pytest.mark.parametrize(
    "frame,kind",
    [
        ("pong", "pong"),
        (b"pong", "pong"),
        ('{"event":"subscribe","arg":{"channel":"liquidation-orders"}}', "subscribed"),
        ('{"event":"unsubscribe","arg":{"channel":"liquidation-orders"}}', "event"),
        ('{"event":"error","code":"60012","msg":"Invalid request"}', "error"),
        ('{"arg":{"channel":"tickers"},"data":[]}', "other"),
        ("garbage", "other"),
    ],
)
def test_okx_control_frames_are_classified_not_parsed(frame, kind):
    assert parse_okx(frame, LINEAR) == (kind, [])


def test_okx_rest_data_parses_like_ws_and_is_marked_rest():
    rest = json.loads(OKX_FRAME)["data"]
    (liq,) = parse_okx_data(rest, LINEAR, "rest")
    (ws_liq,) = parse_okx(OKX_FRAME, LINEAR)[1]
    assert liq.source == "rest"
    # Equal on every stored key: the recorder's dedup relies on it.
    assert liq == ws_liq.__class__(**{**ws_liq.__dict__, "source": "rest"})


def test_okx_contracts_reads_the_instruments_payload():
    payload = {
        "data": [
            {"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "ctMult": "1", "ctType": "linear"},
            {"instId": "BROKEN", "ctVal": "n/a"},
        ]
    }
    assert okx_contracts(payload) == {
        "BTC-USDT-SWAP": OkxContract(ct_val=0.01, ct_mult=1.0, ct_type="linear")
    }


# --- Bybit -------------------------------------------------------------------

# Bybit's own documentation example.
BYBIT_FRAME = (
    '{"topic":"allLiquidation.ROSEUSDT","type":"snapshot","ts":1739502303204,'
    '"data":[{"T":1739502302929,"s":"ROSEUSDT","S":"Sell","v":"20000","p":"0.04499"}]}'
)


def test_bybit_side_is_the_position_so_sell_is_a_liquidated_short():
    # The reverse of Binance/OKX, where Sell is the forced order closing a long.
    kind, (liq,) = parse_bybit(BYBIT_FRAME)
    assert kind == "data"
    assert liq.venue == "bybit"
    assert liq.side == SHORT
    assert liq.size == pytest.approx(20000)
    assert liq.notional == pytest.approx(20000 * 0.04499)
    assert liq.event_ts == datetime.fromtimestamp(1739502302.929, tz=timezone.utc)


def test_bybit_buy_is_a_liquidated_long():
    _, (liq,) = parse_bybit(BYBIT_FRAME.replace('"S":"Sell"', '"S":"Buy"'))
    assert liq.side == LONG


def test_all_three_venues_agree_on_a_liquidated_long():
    # One forced sale of a long, as each venue spells it.
    assert parse_binance(BINANCE_FRAME)[0].side == LONG  # order side SELL
    frame = OKX_FRAME.replace('"posSide":"short","side":"buy"', '"posSide":"long","side":"sell"')
    assert parse_okx(frame, LINEAR)[1][0].side == LONG  # order side sell
    assert parse_bybit(BYBIT_FRAME.replace('"S":"Sell"', '"S":"Buy"'))[1][0].side == LONG


@pytest.mark.parametrize(
    "frame,kind",
    [
        # Real acks, captured 2026-09-29.
        ('{"success":true,"ret_msg":"","conn_id":"x","req_id":"","op":"subscribe"}', "subscribed"),
        ('{"success":true,"ret_msg":"pong","conn_id":"x","req_id":"","op":"ping"}', "pong"),
        ('{"success":false,"ret_msg":"error:handler not found","op":"subscribe"}', "error"),
        ('{"topic":"tickers.BTCUSDT","data":{}}', "other"),
        ("garbage", "other"),
    ],
)
def test_bybit_control_frames_are_classified(frame, kind):
    assert parse_bybit(frame) == (kind, [])


# --- CoverageTracker ---------------------------------------------------------

T0 = 1_790_650_800  # a whole minute


def _tracker(**kwargs):
    """A tracker whose subscription is already known to deliver data."""
    tracker = CoverageTracker(**kwargs)
    tracker.data()
    return tracker


def _proofs(tracker, start, end, every=10):
    t = start
    while t <= end:
        tracker.proof(t)
        t += every


def test_a_minute_is_confirmed_only_once_a_proof_lands_after_it_ends():
    tracker = _tracker()
    _proofs(tracker, T0 - 5, T0 + 55)
    assert tracker.confirmed() == []
    tracker.proof(T0 + 60)
    assert tracker.confirmed() == [(T0, 0)]
    assert tracker.confirmed() == []  # each minute exactly once


def test_a_partial_first_minute_is_never_claimed():
    tracker = _tracker()
    _proofs(tracker, T0 + 7, T0 + 127)
    assert [m for m, _ in tracker.confirmed()] == [T0 + 60]


def test_silence_longer_than_the_limit_breaks_coverage():
    # The shape of a laptop suspend: proofs stop, then resume much later.
    tracker = _tracker(max_silence_s=30)
    _proofs(tracker, T0 - 5, T0 + 125)
    tracker.confirmed()
    tracker.proof(T0 + 125 + 31)  # 31s of silence
    _proofs(tracker, T0 + 160, T0 + 300)
    minutes = [m for m, _ in tracker.confirmed()]
    # T0+120 was open when the silence began, so it is lost. The new stretch
    # starts at T0+156, so its first whole minute is T0+180.
    assert T0 + 120 not in minutes
    assert minutes == [T0 + 180, T0 + 240]


def test_silence_exactly_at_the_limit_keeps_coverage():
    tracker = _tracker(max_silence_s=30)
    tracker.proof(T0 - 1)
    tracker.proof(T0 + 29)
    tracker.proof(T0 + 59)
    tracker.proof(T0 + 60)
    assert tracker.confirmed() == [(T0, 0)]


def test_reset_forgets_the_open_stretch_and_its_counts():
    tracker = _tracker()
    tracker.proof(T0 - 1)
    tracker.count(T0 + 10)
    tracker.reset()
    tracker.proof(T0 + 40)
    tracker.proof(T0 + 60)
    assert tracker.confirmed() == []


def test_counts_land_in_their_receive_minute_and_zero_minutes_are_reported():
    tracker = _tracker()
    tracker.proof(T0 - 1)
    tracker.count(T0 + 5)
    tracker.count(T0 + 50, n=2)
    _proofs(tracker, T0 + 9, T0 + 129)
    assert tracker.confirmed() == [(T0, 3), (T0 + 60, 0)]


def test_counts_before_the_stretch_starts_are_not_attributed():
    tracker = _tracker()
    tracker.proof(T0 + 30)
    tracker.count(T0 + 31)  # in a minute that can't be confirmed
    _proofs(tracker, T0 + 40, T0 + 120)
    assert tracker.confirmed() == [(T0 + 60, 0)]


def test_a_connection_that_never_delivers_data_claims_nothing():
    # Binance's legacy /ws path, 2026-09-29: connected, answered pings, silent.
    tracker = CoverageTracker()
    _proofs(tracker, T0 - 5, T0 + 600)
    assert tracker.confirmed() == []


def test_minutes_proven_before_the_first_data_frame_are_held_not_dropped():
    tracker = CoverageTracker()
    _proofs(tracker, T0 - 5, T0 + 125)
    assert tracker.confirmed() == []
    tracker.data()
    assert [m for m, _ in tracker.confirmed()] == [T0, T0 + 60]


def test_reset_requires_the_new_connection_to_deliver_data_again():
    tracker = _tracker()
    tracker.reset()
    _proofs(tracker, T0 - 5, T0 + 65)
    assert tracker.confirmed() == []
