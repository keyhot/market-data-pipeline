import numpy as np
import pandas as pd
import pytest

from model.zero_shot import (
    cached_predictor,
    context_window,
    forecast_positions,
    p_up_frame,
    p_up_from_quantiles,
    with_forecaster_p,
)

_LEVELS = np.array([0.1, 0.5, 0.9])


def test_a_median_at_the_current_close_is_a_coin_flip():
    assert p_up_from_quantiles(_LEVELS, [99.0, 100.0, 101.0], 100.0) == 0.5


def test_p_up_rises_as_the_current_close_falls_through_the_distribution():
    values = [99.0, 100.0, 101.0]
    closes = np.linspace(98.0, 102.0, 41)
    p = [p_up_from_quantiles(_LEVELS, values, c) for c in closes]
    assert all(a >= b for a, b in zip(p, p[1:]))
    assert p[0] > p[-1]


def test_a_distribution_entirely_above_the_close_clamps_to_the_top_level():
    # Beyond the outermost quantile there is no information: the answer is
    # the most the forecast can say, never 1.0.
    assert p_up_from_quantiles(_LEVELS, [101.0, 102.0, 103.0], 100.0) == pytest.approx(
        0.9
    )
    assert p_up_from_quantiles(_LEVELS, [97.0, 98.0, 99.0], 100.0) == pytest.approx(0.1)


def test_between_quantiles_the_cdf_is_linear():
    # close halfway between q0.5=100 and q0.9=102 -> F = 0.7 -> P(up) = 0.3
    assert p_up_from_quantiles(_LEVELS, [98.0, 100.0, 102.0], 101.0) == pytest.approx(
        0.3
    )


def test_a_tie_with_a_flat_run_of_quantiles_counts_as_down():
    # The label is strict (close[t+h] > close[t]): a forecast mass sitting
    # exactly on the current close is not "up". Right-continuous CDF.
    levels = np.array([0.1, 0.3, 0.5, 0.7, 0.9])
    values = [99.0, 100.0, 100.0, 100.0, 101.0]
    assert p_up_from_quantiles(levels, values, 100.0) == pytest.approx(1 - 0.7)


def test_unsorted_quantile_values_are_rejected():
    with pytest.raises(ValueError):
        p_up_from_quantiles(_LEVELS, [101.0, 100.0, 99.0], 100.0)


def test_context_window_ends_exactly_at_the_forecast_bar():
    closes = np.arange(100.0)
    window = context_window(closes, end_pos=40, context_len=10)
    assert window[-1] == closes[40]
    assert len(window) == 10


def test_context_window_never_sees_a_bar_after_the_forecast_bar():
    rng = np.random.default_rng(0)
    closes = rng.normal(size=200).cumsum() + 100
    before = context_window(closes, end_pos=120, context_len=64).copy()
    tampered = closes.copy()
    tampered[121:] = rng.normal(size=len(tampered) - 121) * 1e6
    assert np.array_equal(context_window(tampered, end_pos=120, context_len=64), before)


def test_a_short_context_raises_rather_than_forecasting_from_less():
    with pytest.raises(ValueError):
        context_window(np.arange(100.0), end_pos=5, context_len=10)


def test_forecast_positions_raises_on_a_timestamp_with_no_bar():
    index = pd.date_range("2026-01-01", periods=5, freq="1min", tz="UTC")
    wanted = pd.DatetimeIndex([index[1], index[3]])
    assert forecast_positions(index, wanted).tolist() == [1, 3]
    with pytest.raises(KeyError):
        forecast_positions(index, wanted.append(pd.DatetimeIndex(
            [pd.Timestamp("2027-01-01", tz="UTC")])))


def _oos(n=4):
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC"),
        "fold": [0] * n,
        "p": [0.9] * n,
        "y": [1, 0, 1, 0][:n],
        "fwd": [0.01, -0.01, 0.02, -0.02][:n],
    })


def test_with_forecaster_p_swaps_only_p_on_identical_rows():
    oos = _oos()
    forecasts = pd.DataFrame({"ts": oos.ts[::-1], "p": [0.1, 0.2, 0.3, 0.4]})
    out = with_forecaster_p(oos, forecasts)
    assert out.ts.tolist() == oos.ts.tolist()
    assert out[["fold", "y", "fwd"]].equals(oos[["fold", "y", "fwd"]])
    assert out.p.tolist() == [0.4, 0.3, 0.2, 0.1]


def test_with_forecaster_p_refuses_a_row_it_has_no_forecast_for():
    oos = _oos()
    with pytest.raises(ValueError):
        with_forecaster_p(oos, pd.DataFrame({"ts": oos.ts[:3], "p": [0.5] * 3}))


def test_p_up_frame_reads_the_quantile_columns_the_collector_wrote():
    forecasts = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=2, freq="1min", tz="UTC"),
        "close": [100.0, 100.0],
        "q0.1": [99.0, 101.0], "q0.5": [100.0, 102.0], "q0.9": [101.0, 103.0],
    })
    out = p_up_frame(forecasts)
    assert out.columns.tolist() == ["ts", "p"]
    assert out.p.tolist() == [pytest.approx(0.5), pytest.approx(0.9)]


def test_cached_predictor_serves_stored_p_for_the_test_rows_only():
    index = pd.date_range("2026-01-01", periods=6, freq="1min", tz="UTC")
    X = pd.DataFrame({"f": range(6)}, index=index)
    predict = cached_predictor(pd.Series([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], index=index))
    assert predict(X, None, slice(0, 2), slice(3, 5)).tolist() == [0.4, 0.5]


def test_cached_predictor_refuses_a_bar_it_has_no_prediction_for():
    index = pd.date_range("2026-01-01", periods=6, freq="1min", tz="UTC")
    X = pd.DataFrame({"f": range(6)}, index=index)
    predict = cached_predictor(pd.Series([0.5] * 4, index=index[:4]))
    with pytest.raises(KeyError, match=r"2 test bars have no prediction"):
        predict(X, None, slice(0, 2), slice(3, 6))


def test_p_up_frame_rearranges_slightly_crossed_quantile_heads():
    forecasts = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=1, freq="1min", tz="UTC"),
        "close": [100.0],
        "q0.1": [99.0], "q0.5": [100.2], "q0.9": [100.1],  # 0.5 head above 0.9
    })
    # sorted: (99.0, 100.1, 100.2) -> close 100 sits in [99.0, 100.1)
    expected = p_up_from_quantiles(_LEVELS, [99.0, 100.1, 100.2], 100.0)
    assert p_up_frame(forecasts).p.tolist() == [pytest.approx(expected)]
