"""E10 helpers: a longer-horizon target scored in positions against a drift
null (vault `Docs/horizon-experiment.md`)."""

import numpy as np
import pandas as pd
import pytest

from model.horizon import (
    drift_per_bar,
    expanding_oos,
    forward_return,
    gate_top_fifth,
    per_call_proxy_bps,
    positions,
    week_block_ci,
)


def _close(n=400, start="2022-12-30", seed=1):
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, n))), index=index)


def test_forward_return_is_the_h_bar_return():
    close = pd.Series([100.0, 101.0, 102.0, 110.0, 120.0])
    ret = forward_return(close, 3)
    assert ret.iloc[0] == pytest.approx(110.0 / 100.0 - 1)
    assert ret.iloc[1] == pytest.approx(120.0 / 101.0 - 1)
    assert ret.iloc[2:].isna().all()


class _Recorder:
    def __init__(self):
        self.lengths = []

    def __call__(self, X, y):
        self.lengths.append(len(X))

        class _Model:
            @staticmethod
            def predict(frame):
                return np.full(len(frame), 0.6)
        return _Model()


def test_expanding_oos_purges_by_h_and_centres_on_the_training_up_rate():
    index = pd.date_range("2021-12-31 20:00", periods=40, freq="15min", tz="UTC")
    X = pd.DataFrame({"f": np.arange(40.0)}, index=index)
    y = pd.Series([1, 1, 1, 0] * 10, index=index)
    ret = pd.Series(0.01, index=index)
    fit = _Recorder()
    oos = expanding_oos(X, y, ret, h=5, years=(2022,), fit=fit)
    first_2022 = int(np.flatnonzero(index.year == 2022)[0])  # 16
    assert fit.lengths == [first_2022 - 5]
    assert oos.centre.iloc[0] == pytest.approx(y.iloc[:first_2022 - 5].mean())
    assert (oos.index.year == 2022).all()
    assert list(oos.columns[:4]) == ["year", "p", "y", "ret"]


def test_gate_is_the_top_fifth_of_centred_confidence_per_year():
    index = pd.date_range("2022-01-01", periods=10, freq="15min", tz="UTC")
    oos = pd.DataFrame({"year": 2022, "p": np.linspace(0.40, 0.80, 10),
                        "centre": 0.6}, index=index)
    gated = gate_top_fifth(oos)
    # |p - 0.6| is largest at the two ends; centring on 0.5 would pick 8 and 9.
    assert np.flatnonzero(gated.to_numpy()).tolist() == [0, 9]


def test_positions_merge_consecutive_calls_and_score_against_the_drift_null():
    close = pd.Series(100.0 * 1.001 ** np.arange(30),
                      index=pd.date_range("2022-01-03", periods=30,
                                          freq="15min", tz="UTC"))
    side = pd.Series(0, index=close.index)
    side.iloc[2:5] = 1      # one long position: calls at 2, 3, 4
    side.iloc[20] = -1      # one short
    mu = 0.001
    pos = positions(side, close, h=3, mu={2022: mu})
    assert len(pos) == 2
    long_ = pos[pos.side == 1].iloc[0]
    assert long_.bars == 4 - 2 + 3
    assert long_.gross == pytest.approx(close.iloc[7] / close.iloc[2] - 1)
    # A pure drift path: a long holds exactly the null, so skill is zero.
    assert long_.skill == pytest.approx(0.0, abs=1e-12)
    short = pos[pos.side == -1].iloc[0]
    assert short.gross == pytest.approx(-(close.iloc[23] / close.iloc[20] - 1))
    assert short.skill == pytest.approx(0.0, abs=1e-12)


def test_a_position_whose_exit_falls_off_the_end_is_dropped():
    close = _close(n=10)
    side = pd.Series(0, index=close.index)
    side.iloc[7] = 1  # exit at row 10, one past the last close
    assert positions(side, close, h=3, mu={2022: 0.0}).empty
    side.iloc[7], side.iloc[6] = 0, 1  # exit at row 9, the last close: kept
    assert len(positions(side, close, h=3, mu={2022: 0.0})) == 1


def test_drift_per_bar_recovers_a_geometric_path():
    close = pd.Series(100.0 * 1.0005 ** np.arange(200),
                      index=pd.date_range("2022-01-01", periods=200,
                                          freq="15min", tz="UTC"))
    assert drift_per_bar(close)[2022] == pytest.approx(0.0005)


def test_per_call_proxy_removes_the_long_bias():
    # Always long in a rising market: signed gross is all drift, proxy is 0.
    index = pd.date_range("2022-01-01", periods=100, freq="15min", tz="UTC")
    oos = pd.DataFrame({"year": 2022, "p": 0.9, "centre": 0.5,
                        "ret": np.linspace(0.001, 0.003, 100)}, index=index)
    assert per_call_proxy_bps(oos, gated=pd.Series(True, index=index)) == \
        pytest.approx(0.0, abs=1e-9)


def test_week_block_ci_brackets_the_mean_and_is_seeded():
    rng = np.random.default_rng(0)
    values = rng.normal(5.0, 1.0, 500)
    weeks = np.repeat(np.arange(50), 10)
    mean, low, high = week_block_ci(values, weeks)
    assert low < mean < high
    assert mean == pytest.approx(values.mean())
    assert week_block_ci(values, weeks) == (mean, low, high)
