"""The expanding, gated fold predictor behind L1 (vault `Docs/pnl-15m-experiment.md`)."""

import numpy as np
import pandas as pd

from model.backtest import BacktestConfig, run_backtest
from model.expanding import ExpandingGatedFold, signed_gross_returns


def _bars(n=3000, seed=3, freq="1h"):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, n)))
    return pd.DataFrame(
        {"Open": close, "High": close * 1.001, "Low": close * 0.999,
         "Close": close, "Volume": rng.integers(100, 10_000, n).astype(float)},
        index=index,
    )


class _Recorder:
    """A fake fit: remembers every training slice, predicts a fixed ramp."""

    def __init__(self):
        self.trained_on = []

    def __call__(self, X, y):
        self.trained_on.append((X.index[0], X.index[-1], len(X)))

        class _Model:
            @staticmethod
            def predict(frame):
                # Deterministic, spread over (0, 1): a p per row's hour.
                return (frame.index.hour.to_numpy() + 0.5) / 24.0
        return _Model()


_CONFIG = BacktestConfig(horizon_bars=1, train_rows=600, test_rows=100,
                         entry_threshold=0.5)


def test_it_never_trains_on_a_row_at_or_past_the_fold():
    fit = _Recorder()
    predictor = ExpandingGatedFold(fit=fit, period="W")
    seen = []

    def spy(X, y, train, test):
        before = len(fit.trained_on)
        out = predictor(X, y, train, test)
        if len(fit.trained_on) > before:
            seen.append((fit.trained_on[-1][1], X.index[train.stop - 1]))
        return out

    run_backtest(_bars(), _CONFIG, predict_fold=spy)
    assert seen, "the predictor never refit"
    for last_trained, last_allowed in seen:
        assert last_trained <= last_allowed


def test_it_trains_on_everything_before_the_fold_not_a_rolling_window():
    fit = _Recorder()
    run_backtest(_bars(), _CONFIG,
                 predict_fold=ExpandingGatedFold(fit=fit, period="W"))
    # The rolling slice's start marches 100 rows a fold; every fit's does not.
    starts = {first for first, _, _ in fit.trained_on}
    assert len(fit.trained_on) > 1 and len(starts) == 1
    sizes = [n for _, _, n in fit.trained_on]
    assert sizes == sorted(sizes) and sizes[-1] > sizes[0]


def test_it_refits_once_per_period_of_the_test_start():
    fit = _Recorder()
    bars = _bars()
    predictor = ExpandingGatedFold(fit=fit, period="W")
    periods = []

    def spy(X, y, train, test):
        periods.append(X.index[test.start].tz_localize(None).to_period("W"))
        return predictor(X, y, train, test)

    run_backtest(bars, _CONFIG, predict_fold=spy)
    assert len(fit.trained_on) == len(set(periods))
    assert len(periods) > len(set(periods)), "fixture too short to test caching"


def test_the_gate_is_the_quantile_of_the_last_fifth_and_the_fit_is_the_rest():
    """The on-air recipe (`model/train.py`): fit on the first 80% minus a
    one-bar purge, gate at GATE_QUANTILE of |p - 0.5| on the last 20%."""
    fit = _Recorder()
    predictor = ExpandingGatedFold(fit=fit, period="W", horizon_bars=1)
    bars = _bars()
    X = pd.DataFrame({"f": 0.0}, index=bars.index)
    y = pd.Series(0, index=bars.index)
    stop = 1000
    out = predictor(X, y, slice(0, stop), slice(stop + 1, stop + 101))

    split = int(stop * 0.8)
    assert fit.trained_on[0][2] == split - 1
    hold = X.index[split:stop]
    expected_cut = np.quantile(np.abs((hold.hour + 0.5) / 24.0 - 0.5), 0.8)
    assert predictor.cutoff == expected_cut

    raw = (X.index[stop + 1: stop + 101].hour.to_numpy() + 0.5) / 24.0
    gated = np.abs(raw - 0.5) >= expected_cut
    assert gated.any() and (~gated).any()
    np.testing.assert_array_equal(out, np.where(gated, raw, 0.5))


def test_it_records_every_raw_call_and_whether_it_was_gated():
    predictor = ExpandingGatedFold(fit=_Recorder(), period="W")
    run_backtest(_bars(), _CONFIG, predict_fold=predictor)
    calls = predictor.calls
    assert calls.index.is_monotonic_increasing and calls.index.is_unique
    assert set(calls.columns) >= {"p", "gated"}
    assert calls.gated.any() and (~calls.gated).any()


def test_an_oracle_feature_still_shows_through_the_wrapper():
    """The harness's leak detector, run through the expanding wrapper with the
    real model: if the wrapper fed the future to the booster the honest run
    would look like this one."""
    bars = _bars(n=2600, seed=5)
    close = bars["Close"]
    leaky = bars.copy()
    leaky["High"] = close * (1.0 + 0.001 + 0.02 * (close.shift(-1) > close))
    config = BacktestConfig(horizon_bars=1, train_rows=600, test_rows=200,
                            entry_threshold=0.5)
    honest = run_backtest(bars, config,
                          predict_fold=ExpandingGatedFold(period="W"))
    oracle = run_backtest(leaky, config,
                          predict_fold=ExpandingGatedFold(period="W"))
    assert oracle["signal_precision"] > 0.9
    assert oracle["signal_precision"] > honest["signal_precision"] + 0.3


def test_signed_returns_merge_each_direction_into_positions():
    index = pd.date_range("2025-01-01", periods=10, freq="15min", tz="UTC")
    close = pd.Series([100, 101, 102, 101, 100, 99, 100, 101, 102, 103.0],
                      index=index)
    # Long on bars 0-1 (one position, exit at bar 2), short on bars 3-4
    # (exit at bar 5), a lone ungated bar 6, long again on bar 8 (exit 9).
    calls = pd.DataFrame({
        "p": [0.7, 0.6, 0.5, 0.3, 0.2, 0.5, 0.9, 0.5, 0.8],
        "gated": [True, True, False, True, True, False, False, False, True],
    }, index=index[:9])
    got = signed_gross_returns(calls, close, horizon_bars=1)
    assert got["long"] == [102 / 100 - 1, 103 / 102 - 1]
    assert got["short"] == [-(99 / 101 - 1)]
