"""E17 DVOL fetcher: pure pagination over Deribit's public
get_volatility_index_data, with an injected getter. No network here --
the response shape below is pinned from a real 2026-10-04 call (rows
[ts_ms, open, high, low, close], newest first, <= 1000 per call,
`continuation` = oldest ts - 1h, null at the end)."""

from __future__ import annotations

import pandas as pd
import pytest

from ingestion import deribit_dvol as dv

H = 3_600_000


def _fake_api(start_ms: int, end_ms: int, page: int = 3):
    """A fake getter serving hourly candles in [start_ms, end_ms], newest
    first, `page` rows per call, with Deribit's continuation semantics."""
    calls = []

    def get_json(url, params):
        calls.append(dict(params))
        lo = max(int(params["start_timestamp"]), start_ms)
        hi = min(int(params["end_timestamp"]), end_ms)
        ts_all = list(range(lo, hi + 1, H))[::-1]  # newest first
        rows = [[t, 50.0, 51.0, 49.0, 50.0 + (t // H) % 7] for t in ts_all[:page]]
        cont = (rows[-1][0] - H) if len(ts_all) > page else None
        return {"jsonrpc": "2.0", "result": {"data": rows, "continuation": cont}}

    return get_json, calls


def test_pagination_follows_continuation_to_the_start():
    start = dv.DVOL_START_MS
    end = start + 9 * H  # 10 candles, 3 per page -> 4 calls
    get_json, calls = _fake_api(start, end, page=3)
    df = dv.fetch_dvol("BTC", start, end, get_json=get_json)
    assert len(df) == 10
    assert len(calls) == 4
    assert df["ts"].is_monotonic_increasing
    assert df["ts"].iloc[0] == pd.Timestamp(start, unit="ms", tz="UTC")
    assert df["ts"].iloc[-1] == pd.Timestamp(end, unit="ms", tz="UTC")
    # each subsequent call's end_timestamp is the previous continuation
    assert int(calls[1]["end_timestamp"]) == int(calls[0]["end_timestamp"]) - 3 * H


def test_request_params_are_public_hourly_and_keyless():
    get_json, calls = _fake_api(dv.DVOL_START_MS, dv.DVOL_START_MS + 2 * H)
    dv.fetch_dvol("ETH", dv.DVOL_START_MS, dv.DVOL_START_MS + 2 * H, get_json=get_json)
    p = calls[0]
    assert p["currency"] == "ETH"
    assert int(p["resolution"]) == 3600
    assert not any(k.lower() in ("api_key", "client_secret", "access_token") for k in p)
    assert dv.DVOL_URL.startswith("https://www.deribit.com/api/v2/public/")


def test_columns_and_utc_candle_start_timestamps():
    get_json, _ = _fake_api(dv.DVOL_START_MS, dv.DVOL_START_MS + H)
    df = dv.fetch_dvol("BTC", dv.DVOL_START_MS, dv.DVOL_START_MS + H, get_json=get_json)
    assert list(df.columns) == ["ts", "open", "high", "low", "close"]
    assert str(df["ts"].dt.tz) == "UTC"


def test_merge_is_idempotent_and_new_rows_win_on_overlap():
    ts = pd.date_range("2021-03-24", periods=4, freq="h", tz="UTC")
    a = pd.DataFrame({"ts": ts[:3], "open": 1.0, "high": 1.0, "low": 1.0, "close": [1.0, 2.0, 3.0]})
    b = pd.DataFrame({"ts": ts[2:], "open": 1.0, "high": 1.0, "low": 1.0, "close": [30.0, 4.0]})
    m = dv.merge_dvol(a, b)
    assert list(m["close"]) == [1.0, 2.0, 30.0, 4.0]
    pd.testing.assert_frame_equal(dv.merge_dvol(m, b), m)
    pd.testing.assert_frame_equal(dv.merge_dvol(m, m), m)


def test_csv_roundtrip(tmp_path):
    ts = pd.date_range("2021-03-24", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({"ts": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": [1.0, 2.0, 3.0]})
    path = tmp_path / "dvol" / "BTC_1h.csv"
    dv.save_dvol_csv(df, path)
    pd.testing.assert_frame_equal(dv.load_dvol_csv(path), df)


def test_missing_cache_loads_empty(tmp_path):
    df = dv.load_dvol_csv(tmp_path / "nope.csv")
    assert df.empty and list(df.columns) == ["ts", "open", "high", "low", "close"]


def test_api_error_raises():
    def get_json(url, params):
        return {"jsonrpc": "2.0", "error": {"code": 10001, "message": "bad"}}

    with pytest.raises(RuntimeError):
        dv.fetch_dvol("BTC", dv.DVOL_START_MS, dv.DVOL_START_MS + H, get_json=get_json)
