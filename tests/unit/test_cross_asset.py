import numpy as np
import pandas as pd

from model.cross_asset import with_cross_features
from model.features import build_features


def _bars(n=400, seed=1, start="2026-01-01"):
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    return pd.DataFrame(
        {"Open": close, "High": close * 1.002, "Low": close * 0.998,
         "Close": close, "Volume": rng.integers(100, 10_000, n).astype(float)},
        index=index,
    )


def test_adds_the_other_symbols_features_prefixed():
    own = build_features(_bars(seed=1), horizon_bars=1)
    X, y = with_cross_features(own.X, own.y, _bars(seed=2))

    base = list(own.X.columns)
    assert list(X.columns) == base + [f"x_{c}" for c in base]
    pd.testing.assert_frame_equal(X[base], own.X.loc[X.index])
    assert y.index.equals(X.index)


def test_the_other_symbols_future_never_reaches_a_row():
    own = build_features(_bars(seed=1), horizon_bars=1)
    other = _bars(seed=2)
    X, _ = with_cross_features(own.X, own.y, other)

    shocked = other.copy()
    cut = other.index[300]
    shocked.loc[shocked.index > cut, ["Open", "High", "Low", "Close"]] *= 3.0
    X_shocked, _ = with_cross_features(own.X, own.y, shocked)

    past = X.index <= cut
    pd.testing.assert_frame_equal(X[past], X_shocked[past])


def test_rows_the_other_symbol_lacks_are_dropped_not_filled():
    own = build_features(_bars(seed=1), horizon_bars=1)
    other = _bars(seed=2).drop(_bars(seed=2).index[200:210])
    X, y = with_cross_features(own.X, own.y, other)

    assert not X.isna().any(axis=None)
    assert len(X) < len(own.X)
    assert X.index.isin(own.X.index).all()
    assert len(y) == len(X)


def test_each_x_column_is_the_other_symbols_feature_at_the_same_timestamp():
    """KI-096: the join key is the bar's own timestamp. The two symbols start
    37 bars apart (so a positional join or a one-bar lag cannot line up by
    luck), and every x_* cell must equal the other symbol's own feature row
    at the SAME index label -- not the bar before, not the bar after."""
    own_bars = _bars(n=400, seed=1)
    other_bars = _bars(n=363, seed=2, start=own_bars.index[37])
    own = build_features(own_bars, horizon_bars=1)
    other = build_features(other_bars, horizon_bars=1).X.add_prefix("x_")

    X, y = with_cross_features(own.X, own.y, other_bars)

    x_cols = [c for c in X.columns if c.startswith("x_")]
    assert x_cols == list(other.columns)
    assert X.index.equals(own.X.index.intersection(other.index))  # exact-label inner join
    pd.testing.assert_frame_equal(X[x_cols], other.loc[X.index])
    pd.testing.assert_series_equal(y, own.y.loc[X.index])
    # The test has teeth only if neighbouring bars really differ.
    assert not X[x_cols].equals(other.shift(1).loc[X.index])
    assert not X[x_cols].equals(other.shift(-1).loc[X.index])
