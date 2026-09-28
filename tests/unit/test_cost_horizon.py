"""E11 helpers: a cost-clearing 3-class label at h = 4 with expanding training
(vault `Docs/cost-horizon-experiment.md`)."""

import numpy as np
import pandas as pd
import pytest

from model.cost_horizon import (
    gate_each_side,
    mechanism,
    three_class_oos,
)
from model.cost_label import DOWN, FLAT, UP


class _Recorder:
    """A fit that remembers its training labels and predicts fixed class
    probabilities [down, flat, up]."""

    def __init__(self, proba):
        self.proba = np.asarray(proba, dtype=float)
        self.labels = []

    def __call__(self, X, labels):
        self.labels.append(np.asarray(labels))
        proba = self.proba

        class _Model:
            @staticmethod
            def predict(frame):
                return np.tile(proba, (len(frame), 1))
        return _Model()


def _frame(n=40, start="2021-12-31 20:00"):
    index = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    X = pd.DataFrame({"f": np.arange(float(n))}, index=index)
    # up, up, flat, down repeating: train share up 0.5, down 0.25.
    ret = pd.Series([0.01, 0.02, 0.0001, -0.01] * (n // 4), index=index)
    return X, ret


def test_three_class_oos_purges_by_h_and_labels_the_training_rows_by_cost():
    X, ret = _frame()
    fit = _Recorder([0.2, 0.3, 0.5])
    oos = three_class_oos(X, ret, cost=0.0022, h=5, years=(2022,), fit=fit)
    first_2022 = int(np.flatnonzero(X.index.year == 2022)[0])  # 16
    assert len(fit.labels) == 1 and len(fit.labels[0]) == first_2022 - 5
    assert fit.labels[0][:4].tolist() == [UP, UP, FLAT, DOWN]
    assert (oos.index.year == 2022).all()


def test_the_centre_is_the_training_up_share_minus_down_share():
    X, ret = _frame()
    fit = _Recorder([0.2, 0.3, 0.5])
    oos = three_class_oos(X, ret, cost=0.0022, h=5, years=(2022,), fit=fit)
    train = ret.iloc[:16 - 5]
    expected = (train > 0.0022).mean() - (train < -0.0022).mean()
    assert oos.centre.iloc[0] == pytest.approx(expected)
    assert oos.centre.iloc[0] != pytest.approx(0.0)
    # The score is P(up) - P(down), and y is the binary sign label.
    assert oos.score.iloc[0] == pytest.approx(0.5 - 0.2)
    assert oos.y.tolist() == (oos.ret > 0).astype(int).tolist()
    assert oos.cls.iloc[:4].tolist() == [UP, UP, FLAT, DOWN]


def test_the_gate_takes_the_top_fifth_within_each_side():
    index = pd.date_range("2022-01-01", periods=30, freq="15min", tz="UTC")
    # 20 rows above the centre, 10 below: imbalanced sides.
    score = np.r_[np.linspace(0.11, 0.30, 20), np.linspace(-0.10, 0.09, 10)]
    oos = pd.DataFrame({"year": 2022, "score": score, "centre": 0.1},
                       index=index)
    side = gate_each_side(oos)
    longs, shorts = np.flatnonzero(side == 1), np.flatnonzero(side == -1)
    # Top fifth of the 20 longs is 4 rows, of the 10 shorts 2 rows; a pooled
    # 20% of |score - centre| would take no shorts at all here.
    assert longs.tolist() == [16, 17, 18, 19]
    assert shorts.tolist() == [20, 21]


def test_the_gate_is_per_year():
    index = pd.DatetimeIndex(["2022-06-01"] * 5 + ["2023-06-01"] * 5, tz="UTC")
    index = index + pd.to_timedelta(np.arange(10), unit="min")
    oos = pd.DataFrame({"year": index.year,
                        "score": [1, 2, 3, 4, 5, 10, 20, 30, 40, 50.0],
                        "centre": 0.0}, index=index)
    side = gate_each_side(oos)
    assert np.flatnonzero(side == 1).tolist() == [4, 9]


def test_mechanism_reads_the_miss_to_hit_size_ratio_and_the_cost_clearing_hit():
    index = pd.date_range("2022-01-01", periods=4, freq="15min", tz="UTC")
    ret = pd.Series([0.0030, 0.0010, 0.0050, -0.0040], index=index)
    side = pd.Series([1, 1, -1, -1], index=index)
    m = mechanism(side, ret, cost=0.0022)
    # hits: +30, +10 and the short's +40 bps; one miss of 50.
    assert m["hit"] == pytest.approx(0.75)
    assert m["miss_hit_ratio"] == pytest.approx(50.0 / (80.0 / 3))
    # The +10 hit does not clear 22 bps; only +30 and +40 do.
    assert m["clearing_hit"] == pytest.approx(0.5)
    side.iloc[0] = 0
    assert mechanism(side, ret, cost=0.0022)["clearing_hit"] == pytest.approx(1 / 3)
