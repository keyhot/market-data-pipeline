import numpy as np
import pandas as pd
import pytest

from model.features import build_features
from model.order_flow import (
    FLOW_FEATURES,
    align_flow,
    flow_features,
    flow_frame,
    leaked_control,
    with_flow_features,
)

# A real BTCUSDT 15m kline, 2026-09-27, verbatim from /api/v3/klines.
_LIVE_ROW = [1790537400000, "84702.00000000", "84796.01000000", "84666.00000000",
             "84771.40000000", "61.89637000", 1790538299999, "5243948.77017360",
             8950, "37.29845000", "3159569.12178380", "0"]


def _raw(n=400, seed=1, start="2026-01-01"):
    rng = np.random.default_rng(seed)
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    rows = []
    for i, c in enumerate(close):
        volume = float(rng.integers(100, 10_000))
        buy = volume * rng.uniform(0.2, 0.8)
        rows.append([t0 + i * 900_000, c, c * 1.002, c * 0.998, c, volume,
                     t0 + (i + 1) * 900_000 - 1, volume * c,
                     int(rng.integers(10, 5_000)), buy, buy * c, "0"])
    return rows


def _bars(raw):
    index = pd.to_datetime([k[0] for k in raw], unit="ms", utc=True)
    return pd.DataFrame(
        {"Open": [float(k[1]) for k in raw], "High": [float(k[2]) for k in raw],
         "Low": [float(k[3]) for k in raw], "Close": [float(k[4]) for k in raw],
         "Volume": [float(k[5]) for k in raw]},
        index=index,
    )


def test_reads_the_verified_binance_field_map():
    frame = flow_frame([_LIVE_ROW])

    row = frame.iloc[0]
    assert frame.index[0] == pd.Timestamp(1790537400000, unit="ms", tz="UTC")
    assert row.volume == 61.89637
    assert row.trades == 8950
    assert row.taker_buy == 37.29845


def test_taker_buy_above_volume_means_the_field_map_is_wrong():
    bad = list(_LIVE_ROW)
    bad[9], bad[5] = bad[5], bad[9]
    with pytest.raises(ValueError, match="taker"):
        flow_frame([bad])


def test_a_volume_weighted_price_outside_the_bar_means_the_field_map_is_wrong():
    bad = list(_LIVE_ROW)
    bad[7] = bad[10]  # taker-buy quote in the quote-volume slot
    with pytest.raises(ValueError, match="quote"):
        flow_frame([bad])


def test_a_fractional_trade_count_means_the_field_map_is_wrong():
    bad = list(_LIVE_ROW)
    bad[8] = "37.29845000"
    with pytest.raises(ValueError, match="trades"):
        flow_frame([bad])


def test_align_accepts_flow_whose_volume_matches_the_stored_bar():
    raw = _raw()
    aligned = align_flow(flow_frame(raw), _bars(raw))
    assert len(aligned) == len(raw)


def test_align_accepts_a_store_that_rounded_volume_to_whole_units():
    # The live price_bars.volume is still BIGINT (migrate_013 unapplied):
    # 202.94 is stored as 203. That is rounding, not misalignment.
    raw = _raw()
    for k in raw:
        k[5] = k[5] + 0.37
        k[7] = k[5] * float(k[4])
        k[9] = min(k[9], k[5])
    bars = _bars(raw)
    bars["Volume"] = bars["Volume"].round()
    assert len(align_flow(flow_frame(raw), bars)) == len(raw)


def test_align_refuses_flow_shifted_by_one_bar_even_against_rounded_volume():
    raw = _raw()
    for k in raw:
        k[5] = k[5] + 0.37
        k[7] = k[5] * float(k[4])
        k[9] = min(k[9], k[5])
    bars = _bars(raw)
    bars["Volume"] = bars["Volume"].round()
    flow = flow_frame(raw)
    shifted = flow.set_axis(flow.index + pd.Timedelta(minutes=15))
    with pytest.raises(ValueError, match="volume"):
        align_flow(shifted, bars)


def test_align_refuses_flow_shifted_by_one_bar():
    raw = _raw()
    flow = flow_frame(raw)
    shifted = flow.set_axis(flow.index + pd.Timedelta(minutes=15))
    with pytest.raises(ValueError, match="volume"):
        align_flow(shifted, _bars(raw))


def test_imbalance_is_plus_one_for_an_all_buy_bar_and_nan_at_zero_volume():
    raw = _raw()
    raw[100][9] = raw[100][5]
    raw[200][5], raw[200][7], raw[200][9], raw[200][10] = 0.0, 0.0, 0.0, 0.0
    feats = flow_features(flow_frame(raw), _bars(raw)["Close"])

    assert list(feats.columns) == list(FLOW_FEATURES)
    stamp = lambda i: pd.Timestamp(raw[i][0], unit="ms", tz="UTC")  # noqa: E731
    assert feats.loc[stamp(100), "flow_imbalance_1"] == 1.0
    assert np.isnan(feats.loc[stamp(200), "flow_imbalance_1"])


def test_the_residual_is_zero_when_flow_is_exactly_its_return():
    raw = _raw()
    close = _bars(raw)["Close"]
    r = np.log(close / close.shift(1)).fillna(0.0).to_numpy()
    for k, ret in zip(raw, r):
        k[9] = k[5] * (0.5 + 50 * ret) / 1.0  # imbalance = 100·r, linear in r
    feats = flow_features(flow_frame(raw), close)

    resid = feats["flow_resid_1"].dropna()
    assert len(resid) > 200
    assert np.abs(resid).max() < 1e-9


def test_the_future_never_reaches_a_feature_row():
    raw = _raw()
    feats = flow_features(flow_frame(raw), _bars(raw)["Close"])
    cut = 300
    short = flow_features(flow_frame(raw[: cut + 1]), _bars(raw[: cut + 1])["Close"])

    pd.testing.assert_frame_equal(feats.iloc[: cut + 1], short)


def test_rows_with_missing_flow_are_dropped_from_the_joined_frame_not_filled():
    raw = _raw()
    raw[250][5], raw[250][7], raw[250][9], raw[250][10] = 0.0, 0.0, 0.0, 0.0
    own = build_features(_bars(raw), horizon_bars=1)
    feats = flow_features(flow_frame(raw), _bars(raw)["Close"])
    X, y = with_flow_features(own.X, own.y, feats.drop(feats.index[100:110]))

    base = list(own.X.columns)
    assert list(X.columns) == base + list(FLOW_FEATURES)
    assert not X.isna().any(axis=None)
    assert not X.index.isin(feats.index[100:110]).any()
    assert pd.Timestamp(raw[250][0], unit="ms", tz="UTC") not in X.index
    pd.testing.assert_frame_equal(X[base], own.X.loc[X.index])
    assert y.index.equals(X.index)


def test_the_positive_control_reads_the_next_bars_imbalance():
    raw = _raw()
    feats = flow_features(flow_frame(raw), _bars(raw)["Close"])
    leaked = leaked_control(feats)

    pd.testing.assert_series_equal(
        leaked["flow_imbalance_1"].iloc[:-1],
        feats["flow_imbalance_1"].shift(-1).iloc[:-1],
    )
    pd.testing.assert_frame_equal(leaked.drop(columns="flow_imbalance_1"),
                                  feats.drop(columns="flow_imbalance_1"))
