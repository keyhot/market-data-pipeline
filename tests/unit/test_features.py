import numpy as np
import pandas as pd
import pytest

from model.features import build_features, build_latest_features


def _bars(n=300, seed=7):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-07-01", periods=n, freq="1min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    return pd.DataFrame(
        {
            "Open": close * (1 + rng.normal(0, 0.0002, n)),
            "High": close * 1.001,
            "Low": close * 0.999,
            "Close": close,
            "Volume": rng.integers(100, 10_000, n).astype(float),
        },
        index=index,
    )


def test_features_and_labels_align():
    result = build_features(_bars(), horizon_bars=15)

    assert len(result.X) == len(result.y)
    assert not result.X.isna().any(axis=None)
    assert set(result.y.unique()) <= {0, 1}
    assert result.feature_names == list(result.X.columns)


def test_label_is_forward_return_sign():
    bars = _bars()
    horizon = 15
    result = build_features(bars, horizon_bars=horizon)

    ts = result.X.index[0]
    close = bars["Close"]
    pos = close.index.get_loc(ts)
    expected = int(close.iloc[pos + horizon] > close.iloc[pos])
    assert result.y.loc[ts] == expected


def test_no_lookahead_property():
    """Features at time t must be identical when computed on data truncated
    at t — the guarantee that nothing leaks from the future."""
    bars = _bars()
    full = build_features(bars, horizon_bars=15)

    cut = 250
    truncated_bars = bars.iloc[:cut]
    truncated = build_features(truncated_bars, horizon_bars=15)

    common = truncated.X.index.intersection(full.X.index)
    assert len(common) > 50
    pd.testing.assert_frame_equal(full.X.loc[common], truncated.X.loc[common])


def test_label_rows_past_data_end_are_dropped():
    bars = _bars(n=300)
    result = build_features(bars, horizon_bars=15)

    # the last `horizon` bars can't have labels — they must not appear
    assert result.X.index.max() <= bars.index[-16]


def test_store_reader_shape_accepted():
    bars = _bars(n=200)
    store_shaped = pd.DataFrame(
        {
            "timestamp": [ts.isoformat() for ts in bars.index],
            "open": bars["Open"].values,
            "high": bars["High"].values,
            "low": bars["Low"].values,
            "close": bars["Close"].values,
            "volume": bars["Volume"].values,
        }
    )

    provider_result = build_features(bars, horizon_bars=10)
    store_result = build_features(store_shaped, horizon_bars=10)

    # index name/freq metadata differs between the shapes; values must not
    pd.testing.assert_frame_equal(
        provider_result.X, store_result.X, check_names=False, check_freq=False
    )


def test_missing_columns_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        build_features(pd.DataFrame({"Close": [1.0, 2.0]}))


def test_latest_features_single_complete_row():
    latest = build_latest_features(_bars())

    assert latest is not None
    assert len(latest) == 1
    assert not latest.isna().any(axis=None)


def test_latest_features_none_when_history_too_short():
    assert build_latest_features(_bars(n=30)) is None


def test_unsorted_input_is_sorted():
    bars = _bars().iloc[::-1]  # reversed order
    result = build_features(bars, horizon_bars=10)

    assert result.X.index.is_monotonic_increasing
