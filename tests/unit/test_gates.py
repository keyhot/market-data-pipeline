import numpy as np
import pandas as pd

from model.gates import always_long, volatility_gate


def _frame(vol):
    index = pd.date_range("2026-01-01", periods=len(vol), freq="1min", tz="UTC")
    return pd.DataFrame({"volatility_60": vol}, index=index)


def _inner(X, y, train, test):
    return np.full(test.stop - test.start, 0.9)


def test_bars_below_the_train_window_cutoff_are_flat_and_the_rest_keep_p():
    # train vol 1..10 -> top 20% cutoff is the 0.8 quantile (8.2)
    X = _frame(list(range(1, 11)) + [5.0, 9.0, 20.0, 8.0])
    predict = volatility_gate(_inner, "volatility_60", top_fraction=0.2)
    p = predict(X, None, slice(0, 10), slice(10, 14))
    assert p.tolist() == [0.0, 0.9, 0.9, 0.0]


def test_the_cutoff_never_reads_the_test_window():
    # A gate cut from the full series would leak future volatility into
    # today's rule. Blow up the test bars' vol: the cutoff must not move.
    calm = _frame(list(range(1, 11)) + [9.0, 9.0])
    wild = _frame(list(range(1, 11)) + [9.0, 1e6])
    predict = volatility_gate(_inner, "volatility_60", top_fraction=0.2)
    assert predict(calm, None, slice(0, 10), slice(10, 11)).tolist() == \
        predict(wild, None, slice(0, 10), slice(10, 11)).tolist()


def test_the_cutoff_moves_with_the_train_window():
    X = _frame([1.0] * 10 + [100.0] * 10 + [50.0])
    predict = volatility_gate(_inner, "volatility_60", top_fraction=0.5)
    assert predict(X, None, slice(0, 10), slice(20, 21)).tolist() == [0.9]
    assert predict(X, None, slice(10, 20), slice(20, 21)).tolist() == [0.0]


def test_a_full_gate_is_the_inner_predictor_unchanged():
    X = _frame(list(range(1, 15)))
    predict = volatility_gate(_inner, "volatility_60", top_fraction=1.0)
    assert predict(X, None, slice(0, 10), slice(10, 14)).tolist() == [0.9] * 4


def test_always_long_is_certain_on_every_test_bar():
    X = _frame(list(range(1, 15)))
    assert always_long(X, None, slice(0, 10), slice(10, 14)).tolist() == [1.0] * 4
