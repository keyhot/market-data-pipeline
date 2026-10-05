import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestion.liquidations import (
    LONG,
    SHORT,
    CoverageTracker,
    KrakenContract,
    KrakenTrade,
    OkxContract,
    aggregate_kraken_minutes,
    kraken_contracts,
    kraken_coverage_minutes,
    kraken_gap,
    kraken_liquidations,
    kraken_new_trades,
    okx_contracts,
    parse_binance,
    parse_kraken_history,
    parse_okx,
    parse_okx_data,
    parse_bybit,
)

KRAKEN_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures" / "liquidations" / "kraken_trade_history_pf_xbtusd.json"
)
KRAKEN_INSTRUMENTS_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures" / "liquidations" / "kraken_instruments.json"
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


# --- Kraken --------------------------------------------------------------

# Real response, captured 2026-10-05 from
# GET https://futures.kraken.com/derivatives/api/v3/history?symbol=PF_XBTUSD
# (see the fixture file's _comment for what it pins: numeric size/price,
# ascending time order, trade_id counting down within the window).
KRAKEN_PAYLOAD = json.loads(KRAKEN_FIXTURE.read_text())

KRAKEN_LIQUIDATION_ROW = {
    "time": "2026-10-05T15:49:10.000000Z",
    "trade_id": 1,
    "price": 85000.0,
    "size": 1.5,
    "side": "sell",
    "type": "liquidation",
    "uid": "11111111-1111-1111-1111-111111111111",
    "sequence_id": "999",
}


def test_parse_kraken_history_reads_every_real_row():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    assert len(trades) == len(KRAKEN_PAYLOAD["history"])
    first = trades[0]
    assert first.symbol == "PF_XBTUSD"
    assert first.uid == "6c4a50dd-19bd-4ede-81c0-260ec996cdb3"
    assert first.type == "fill"
    assert first.side == "sell"
    assert first.price == pytest.approx(85360)
    assert first.size == pytest.approx(0.0005)
    assert first.event_ts == datetime(2026, 10, 5, 15, 48, 56, 419439, tzinfo=timezone.utc)


def test_parse_kraken_history_real_sample_has_no_liquidations():
    # Confirms the fixture is what it claims to be: an ordinary busy window.
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    assert kraken_liquidations(trades) == []


def test_parse_kraken_history_array_is_ascending_not_descending():
    # Pins the seam bug: docs.kraken.com claims descending-by-time order;
    # the real response is ascending (oldest first).
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    assert trades[0].event_ts < trades[-1].event_ts


def test_kraken_natural_key_collides_but_uid_does_not():
    # The schema fact that justifies venue_trade_id/uq_liquidations_natural's
    # WHERE clause (scripts/migrate_018.sql): on a busy window, several
    # distinct real fills share (event_ts, side, price, size) — the old
    # natural key, applied to Kraken, would have silently merged them.
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    natural_keys = [(t.event_ts, t.side, t.price, t.size) for t in trades]
    uids = [t.uid for t in trades]
    assert len(set(natural_keys)) < len(trades)
    assert len(set(uids)) == len(trades)


def test_kraken_trade_id_is_not_stable_across_the_window():
    # Pins the other seam bug: trade_id counts DOWN as time goes up, so it
    # cannot be a persistent cross-poll id — uid is the only safe key.
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    ids_by_time = [t.raw["trade_id"] for t in sorted(trades, key=lambda t: t.event_ts)]
    assert ids_by_time == sorted(ids_by_time, reverse=True)


@pytest.mark.parametrize(
    "row",
    [
        {"time": "not-a-time", "uid": "x", "price": "1", "size": "1", "side": "sell"},
        {"time": "2026-10-05T00:00:00Z", "uid": "x", "price": "bad", "size": "1", "side": "sell"},
        {"uid": "x", "price": "1", "size": "1", "side": "sell"},  # missing time
    ],
)
def test_parse_kraken_history_drops_malformed_rows_without_failing(row):
    assert parse_kraken_history({"history": [row]}, "PF_XBTUSD") == []


def test_kraken_liquidation_type_sell_closes_a_long():
    trades = parse_kraken_history({"history": [KRAKEN_LIQUIDATION_ROW]}, "PF_XBTUSD")
    (liq,) = kraken_liquidations(trades)
    assert liq.venue == "kraken"
    assert liq.symbol == "PF_XBTUSD"
    assert liq.side == LONG
    assert liq.price == pytest.approx(85000.0)
    assert liq.size == pytest.approx(1.5)
    assert liq.size_unit == "contracts"
    assert liq.source == "rest"
    assert liq.venue_trade_id == "11111111-1111-1111-1111-111111111111"


def test_kraken_liquidation_type_buy_closes_a_short():
    row = {**KRAKEN_LIQUIDATION_ROW, "side": "buy"}
    trades = parse_kraken_history({"history": [row]}, "PF_XBTUSD")
    (liq,) = kraken_liquidations(trades)
    assert liq.side == SHORT


def test_kraken_partial_liquidation_type_is_also_kept():
    row = {**KRAKEN_LIQUIDATION_ROW, "type": "partial liquidation"}
    trades = parse_kraken_history({"history": [row]}, "PF_XBTUSD")
    assert len(kraken_liquidations(trades)) == 1


def test_kraken_plain_fills_are_not_liquidations():
    row = {**KRAKEN_LIQUIDATION_ROW, "type": "fill"}
    trades = parse_kraken_history({"history": [row]}, "PF_XBTUSD")
    assert kraken_liquidations(trades) == []


def test_kraken_new_trades_forwards_everything_on_the_first_poll():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    assert kraken_new_trades(trades, None) == trades


def test_kraken_new_trades_filters_to_strictly_after_last_ts():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    midpoint = sorted(t.event_ts for t in trades)[len(trades) // 2]
    new = kraken_new_trades(trades, midpoint)
    assert new and all(t.event_ts > midpoint for t in new)


def test_kraken_gap_false_on_first_poll_with_no_prior_state():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    assert kraken_gap(trades, None) is False


def test_kraken_gap_false_when_oldest_row_reaches_back_to_last_ts():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    oldest = min(t.event_ts for t in trades)
    assert kraken_gap(trades, oldest) is False


def test_kraken_gap_true_when_more_than_a_window_landed_between_polls():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    oldest = min(t.event_ts for t in trades)
    from datetime import timedelta

    last_ts = oldest - timedelta(seconds=1)  # a trade existed before this window
    assert kraken_gap(trades, last_ts) is True


# --- K2: contract sizing (base-unit resolution) -------------------------------


def test_kraken_contracts_reads_the_real_instruments_fixture():
    payload = json.loads(KRAKEN_INSTRUMENTS_FIXTURE.read_text())
    contracts = kraken_contracts(payload)
    assert contracts["PF_XBTUSD"] == KrakenContract(contract_size=1.0, kind="flexible_futures")
    assert contracts["PF_ETHUSD"] == KrakenContract(contract_size=1.0, kind="flexible_futures")


def test_kraken_contracts_drops_malformed_rows():
    payload = {"instruments": [{"symbol": "BROKEN"}]}
    assert kraken_contracts(payload) == {}


def test_kraken_liquidation_resolves_base_units_when_contract_known():
    trades = parse_kraken_history({"history": [KRAKEN_LIQUIDATION_ROW]}, "PF_XBTUSD")
    contract = KrakenContract(contract_size=1.0, kind="flexible_futures")
    (liq,) = kraken_liquidations(trades, contract)
    assert liq.size_unit == "base"
    assert liq.qty_base == pytest.approx(1.5)
    assert liq.notional == pytest.approx(1.5 * 85000.0)


def test_kraken_liquidation_without_contract_stays_conservative():
    trades = parse_kraken_history({"history": [KRAKEN_LIQUIDATION_ROW]}, "PF_XBTUSD")
    (liq,) = kraken_liquidations(trades)  # no contract arg, same as before
    assert liq.size_unit == "contracts"
    assert liq.qty_base is None
    assert liq.notional is None


# --- K2: per-minute all-trade aggregate ---------------------------------------


def test_aggregate_kraken_minutes_groups_by_symbol_and_minute():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    aggs = aggregate_kraken_minutes(trades)
    # The fixture's 8 rows span two distinct minutes: 15:48 (4 rows at
    # 85360/85360/85361/85361) and 15:49 (4 rows at 85328/85327/85327/85327).
    assert len(aggs) == 2
    by_min_price = {a.min_price: a for a in aggs}
    m48 = by_min_price[85360.0]
    assert m48.max_price == 85361.0
    assert m48.trade_count == 4
    assert m48.total_size == pytest.approx(0.0005 + 0.0002 + 0.0002 + 0.0002)
    assert m48.liq_count == 0 and m48.liq_size == 0.0
    m49 = by_min_price[85327.0]
    assert m49.max_price == 85328.0
    assert m49.trade_count == 4
    assert m49.total_size == pytest.approx(0.0005 + 0.0011 + 0.0001 + 0.0001)


def test_aggregate_kraken_minutes_near_wick_size_within_tolerance():
    rows = [
        {"time": "2026-01-01T00:00:00Z", "uid": "a", "price": 100.0, "size": 1.0, "side": "sell", "type": "fill"},
        {"time": "2026-01-01T00:00:01Z", "uid": "b", "price": 100.019, "size": 2.0, "side": "sell", "type": "fill"},  # ~1.9bps from 100
        {"time": "2026-01-01T00:00:02Z", "uid": "c", "price": 110.0, "size": 3.0, "side": "sell", "type": "fill"},  # far from min, is the max
    ]
    trades = parse_kraken_history({"history": rows}, "PF_XBTUSD")
    (agg,) = aggregate_kraken_minutes(trades)
    assert agg.min_price == 100.0
    assert agg.max_price == 110.0
    # a (0bps) and b (~1.9bps) both count toward size_near_min; c does not.
    assert agg.size_near_min == pytest.approx(3.0)
    # only c (the max itself) counts toward size_near_max.
    assert agg.size_near_max == pytest.approx(3.0)


def test_aggregate_kraken_minutes_counts_liquidation_rows_separately():
    rows = [
        {"time": "2026-01-01T00:00:00Z", "uid": "a", "price": 100.0, "size": 1.0, "side": "sell", "type": "fill"},
        {"time": "2026-01-01T00:00:01Z", "uid": "b", "price": 99.0, "size": 2.0, "side": "sell", "type": "liquidation"},
    ]
    trades = parse_kraken_history({"history": rows}, "PF_XBTUSD")
    (agg,) = aggregate_kraken_minutes(trades)
    assert agg.trade_count == 2
    assert agg.liq_count == 1
    assert agg.liq_size == pytest.approx(2.0)


def test_aggregate_kraken_minutes_is_pure_and_does_not_dedupe_itself():
    # Dedup is the caller's job; handing the same trade twice double-counts
    # here by design, which is exactly why the recorder dedupes by uid
    # before calling this.
    rows = [{"time": "2026-01-01T00:00:00Z", "uid": "a", "price": 100.0, "size": 1.0,
             "side": "sell", "type": "fill"}]
    trades = parse_kraken_history({"history": rows}, "PF_XBTUSD")
    (agg,) = aggregate_kraken_minutes(trades + trades)
    assert agg.trade_count == 2
    assert agg.total_size == pytest.approx(2.0)


# --- K2: per-symbol gap-aware coverage -----------------------------------------


def test_kraken_coverage_minutes_empty_on_first_poll():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    assert kraken_coverage_minutes(None, trades, False) == []


def test_kraken_coverage_minutes_empty_when_nothing_new_has_elapsed():
    trades = parse_kraken_history(KRAKEN_PAYLOAD, "PF_XBTUSD")
    newest = max(t.event_ts for t in trades)
    assert kraken_coverage_minutes(newest, trades, False) == []


def test_kraken_coverage_minutes_covers_the_stretch_since_last_poll():
    rows = [
        {"time": "2026-01-01T00:00:30Z", "uid": "a", "price": 1, "size": 1, "side": "sell", "type": "fill"},
        {"time": "2026-01-01T00:05:10Z", "uid": "b", "price": 1, "size": 1, "side": "sell", "type": "fill"},
    ]
    trades = parse_kraken_history({"history": rows}, "PF_XBTUSD")
    last_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    minutes = kraken_coverage_minutes(last_ts, trades, False)
    base = int(last_ts.timestamp())
    # Whole minutes 00:01..00:04 have elapsed before the newest trade's own
    # (still-open) minute 00:05; 00:00 isn't re-claimed (last_ts already in it).
    assert [m - base for m, covered, _ in minutes] == [60, 120, 180, 240]
    assert all(covered for _, covered, _ in minutes)
    # rows_received counts trades landing in that minute; 00:00:30 is in the
    # NOT-emitted minute 0, so every emitted minute here is quiet (0 rows).
    assert all(n == 0 for _, _, n in minutes)


def test_kraken_coverage_minutes_marks_the_whole_stretch_uncovered_on_a_gap():
    rows = [
        {"time": "2026-01-01T00:03:00Z", "uid": "a", "price": 1, "size": 1, "side": "sell", "type": "fill"},
    ]
    trades = parse_kraken_history({"history": rows}, "PF_XBTUSD")
    last_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    minutes = kraken_coverage_minutes(last_ts, trades, True)
    base = int(last_ts.timestamp())
    assert [(m - base, covered, n) for m, covered, n in minutes] == [
        (60, False, 0),
        (120, False, 0),
    ]


def test_kraken_gap_false_on_an_empty_poll():
    assert kraken_gap([], datetime(2026, 1, 1, tzinfo=timezone.utc)) is False


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
