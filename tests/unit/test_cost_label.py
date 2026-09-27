import numpy as np
import pandas as pd
import pytest

from model.backtest import BacktestConfig, run_backtest
from model.cost_label import (
    DOWN,
    FLAT,
    UP,
    cost_class,
    cost_label_fold,
    forward_returns,
    oracle_fold,
    round_trip_cost,
)
from model.features import build_features


def _bars(n=400, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "open": close, "high": close * 1.001, "low": close * 0.999,
        "close": close, "volume": rng.uniform(1, 2, n),
    }, index=index)


def test_the_bands_are_strict_so_a_move_of_exactly_the_cost_is_flat():
    fwd = pd.Series([0.0030, 0.0022, 0.0021, 0.0, -0.0022, -0.0023])
    assert cost_class(fwd, 0.0022).tolist() == [UP, FLAT, FLAT, FLAT, FLAT, DOWN]


def test_the_cost_is_the_configs_round_trip_not_a_literal():
    assert round_trip_cost(BacktestConfig()) == pytest.approx(0.0022)
    cheap = BacktestConfig(fee_per_side=0.0002, slippage=0.0)
    assert round_trip_cost(cheap) == pytest.approx(0.0004)
    fwd = pd.Series([0.001])
    assert cost_class(fwd, round_trip_cost(BacktestConfig())).tolist() == [FLAT]
    assert cost_class(fwd, round_trip_cost(cheap)).tolist() == [UP]


def test_the_forward_return_agrees_in_sign_with_the_binary_label_row_for_row():
    # Same definition as build_features, reindexed on its rows — the check
    # that the closure's labels line up with X at all.
    bars = _bars()
    features = build_features(bars, horizon_bars=16)
    fwd = forward_returns(bars, 16, features.X.index)
    assert len(fwd) == len(features.X)
    assert ((fwd > 0).astype(int).to_numpy() == features.y.to_numpy()).all()


def test_the_forward_return_at_t_reads_only_close_t_and_close_t_plus_h():
    bars = _bars()
    index = bars.index[100:101]
    before = forward_returns(bars, 16, index).iloc[0]
    touched = bars.copy()
    others = [i for i in range(len(bars)) if i not in (100, 116)]
    touched.iloc[others, touched.columns.get_loc("close")] *= 3.0
    assert forward_returns(touched, 16, index).iloc[0] == pytest.approx(before)
    touched.iloc[116, touched.columns.get_loc("close")] *= 1.5
    assert forward_returns(touched, 16, index).iloc[0] != pytest.approx(before)


def _separable(n=600):
    # A feature that names the class outright, so a correct fit is checkable.
    index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    classes = np.tile([DOWN, FLAT, UP], n // 3)
    X = pd.DataFrame({"f": classes.astype(float)}, index=index)
    fwd = pd.Series(np.select([classes == UP, classes == DOWN], [0.01, -0.01], 0.0),
                    index=index)
    return X, fwd, classes


def test_the_fold_goes_long_exactly_where_it_predicts_up():
    X, fwd, classes = _separable()
    predict = cost_label_fold(fwd, 0.0022)
    out = predict(X, None, slice(0, 450), slice(450, 600))
    assert out.tolist() == (classes[450:600] == UP).astype(float).tolist()


def test_the_fold_never_reads_labels_from_the_test_window():
    X, fwd, _ = _separable()
    lying = fwd.copy()
    lying.iloc[450:] = -lying.iloc[450:]
    a = cost_label_fold(fwd, 0.0022)(X, None, slice(0, 450), slice(450, 600))
    b = cost_label_fold(lying, 0.0022)(X, None, slice(0, 450), slice(450, 600))
    assert a.tolist() == b.tolist()


def test_a_train_window_missing_a_class_still_fits_three():
    # A calm 800-row window can hold no `down` at all; without num_class the
    # booster would silently re-number the classes it did see.
    X, fwd, classes = _separable()
    no_down = np.flatnonzero(classes != DOWN)
    X2, fwd2 = X.iloc[no_down], fwd.iloc[no_down]
    out = cost_label_fold(fwd2, 0.0022)(X2, None, slice(0, 300), slice(300, 400))
    expected = (classes[no_down][300:400] == UP).astype(float)
    assert out.tolist() == expected.tolist()


def test_the_oracle_is_the_true_up_class():
    X, fwd, classes = _separable()
    out = oracle_fold(fwd, 0.0022)(X, None, slice(0, 450), slice(450, 600))
    assert out.tolist() == (classes[450:600] == UP).astype(float).tolist()


def _oracle_run(shift=0):
    bars = _bars(n=2000, seed=3)
    config = BacktestConfig(horizon_bars=16, train_rows=300, test_rows=100)
    X = build_features(bars, horizon_bars=16).X
    fwd = forward_returns(bars, 16, X.index)
    if shift:
        fwd = pd.Series(np.roll(fwd.to_numpy(), shift), index=fwd.index)
    result = run_backtest(bars, config,
                          predict_fold=oracle_fold(fwd, round_trip_cost(config)))
    traded = [f for f in result["folds"] if f["positions"]]
    beat = sum(f["strategy_return"] > f["null_return"] for f in traded)
    return beat / len(traded), result["avg_position_return"]


def test_the_oracle_beats_its_null_through_the_real_harness():
    # The control that must differ. Not every fold: a run of up-bars merges
    # into one position that exits h after its LAST bar (KI-001), so even
    # perfect foresight can give some back. ~88% on random walks.
    share, avg = _oracle_run()
    assert share >= 0.8
    assert avg > 0


def test_a_misaligned_oracle_fails_the_same_bar():
    # Proof the bar above can fail: foresight shifted off its rows is noise.
    share, avg = _oracle_run(shift=500)
    assert share < 0.8 or avg <= 0
