import numpy as np
import pandas as pd
import pytest

from model.features import build_features, build_latest_features, roll_bars


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


# --- roll_bars: the live 15m model reads 1m bars rolled to end *now* ---------


def _ohlc_1m(n, start="2026-07-01 00:00", seed=3):
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    spread = np.abs(rng.normal(0, 0.001, (2, n)))
    return pd.DataFrame(
        {
            "Open": close * (1 + rng.normal(0, 0.0003, n)),
            "High": close * (1 + spread[0]) + 0.01,
            "Low": close * (1 - spread[1]) - 0.01,
            "Close": close,
            "Volume": rng.integers(100, 10_000, n).astype(float),
        },
        index=index,
    )


def test_roll_bars_on_a_boundary_equals_the_exchange_15m_bars():
    # 1,200 minutes from 00:00 ends at 19:59, so "now" is a 15m boundary and
    # the rolled bars must be exactly the clock-aligned 15m bars the model
    # was trained on — values and all, only the stamp convention differs.
    minutes = _ohlc_1m(1200)
    rolled = roll_bars(minutes, 15)

    frame = minutes.rename(columns=str.lower)
    exchange = frame.resample("15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "volume": "sum"}
    )
    assert len(rolled) == len(exchange) == 80
    np.testing.assert_allclose(
        rolled[["open", "high", "low", "close", "volume"]].to_numpy(),
        exchange[["open", "high", "low", "close", "volume"]].to_numpy(),
    )


def test_roll_bars_last_bar_is_the_last_fifteen_minutes_off_boundary():
    # 1,207 minutes: "now" is 7 minutes past a boundary, the live case.
    minutes = _ohlc_1m(1207)
    rolled = roll_bars(minutes, 15)
    last15 = minutes.iloc[-15:]

    last = rolled.iloc[-1]
    assert last["open"] == last15["Open"].iloc[0]
    assert last["high"] == last15["High"].max()
    assert last["low"] == last15["Low"].min()
    assert last["close"] == last15["Close"].iloc[-1]
    assert last["volume"] == pytest.approx(last15["Volume"].sum())
    # Stamped with the newest minute it contains: the signal is "as of now".
    assert rolled.index[-1] == minutes.index[-1]
    assert (rolled.index.to_series().diff().dropna()
            == pd.Timedelta(minutes=15)).all()


def test_roll_bars_drops_a_partial_head_bin():
    # 1,207 minutes = 80 full bins + 7 leftover minutes at the start.
    rolled = roll_bars(_ohlc_1m(1207), 15)
    assert len(rolled) == 80


def test_roll_bars_never_reads_a_future_minute():
    minutes = _ohlc_1m(1207)
    full = roll_bars(minutes, 15)
    truncated = roll_bars(minutes.iloc[:-15], 15)
    # Dropping the newest 15 minutes drops exactly the newest rolled bar.
    pd.testing.assert_frame_equal(full.iloc[:-1], truncated, check_freq=False)


def test_rolled_latest_features_match_the_training_features_on_a_boundary():
    minutes = _ohlc_1m(1200)
    frame = minutes.rename(columns=str.lower)
    exchange = frame.resample("15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "volume": "sum"}
    )
    live = build_latest_features(roll_bars(minutes, 15))
    trained = build_features(exchange, horizon_bars=1)
    # The training rows stop one bar early (the label needs the next close),
    # so compare the feature computation on the same window instead.
    expected = build_latest_features(exchange)
    np.testing.assert_allclose(live.to_numpy(), expected.to_numpy())
    assert list(live.columns) == trained.feature_names
