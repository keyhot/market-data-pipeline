"""P1 plumbing in `scripts/pnl_15m_report.py`: the data.binance.vision kline
parser and the prediction replay that lets every cost profile score one set
of calls. Both are pure; nothing here downloads or trains."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from pnl_15m_report import Replay, parse_vision_klines  # noqa: E402

_HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
           "count,taker_buy_volume,taker_buy_quote_volume,ignore\n")
_ROW = "1577836800000,7189.43,7190.52,7170.15,7171.55,2449.049,1577837699999,0,0,0,0,0\n"


def test_a_header_row_is_skipped_and_a_headerless_file_is_read():
    assert parse_vision_klines(_HEADER + _ROW) == parse_vision_klines(_ROW)
    assert parse_vision_klines(_ROW) == [[
        "2020-01-01T00:00:00+00:00", "7189.43", "7190.52", "7170.15",
        "7171.55", "2449.049"]]


def test_microsecond_open_times_land_on_the_same_bar():
    micro = _ROW.replace("1577836800000", "1577836800000000", 1)
    assert parse_vision_klines(micro)[0][0] == "2020-01-01T00:00:00+00:00"


def test_replay_calls_the_predictor_once_per_fold_and_repeats_its_answer():
    calls = []

    def predictor(X, y, train, test):
        calls.append(test.start)
        return np.full(test.stop - test.start, 0.6 + len(calls))

    replay = Replay(predictor)
    first = replay(None, None, slice(0, 5), slice(6, 8))
    again = replay(None, None, slice(0, 5), slice(6, 8))
    replay(None, None, slice(0, 7), slice(8, 10))
    assert calls == [6, 8]
    np.testing.assert_array_equal(first, again)
