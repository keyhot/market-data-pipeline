import numpy as np
import pandas as pd
import pytest

from model.archive import (
    DEPTH_FEATURES,
    METRICS_FEATURES,
    METRICS_STEP,
    asof,
    check_stamp_convention,
    depth_features,
    leaked_control,
    metrics_features,
    parse_depth,
    parse_metrics,
    with_archive_features,
)

# Verbatim from BTCUSDT-metrics-2020-09-01.zip: the header and a duplicated row.
_METRICS_CSV = """create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio
2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,1.17547937,1.23012681,1.35731217,0.78373373
2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,1.17547937,1.23012681,1.35731217,0.78373373
2020-09-01 00:05:00,BTCUSDT,39106.41300000,455693833.60853225,1.17905012,1.22928510,1.35924435,0.85063183
"""


def _grid(n, start="2025-01-01", freq="15min"):
    return pd.date_range(start, periods=n, freq=freq, tz="UTC")


def _metrics(n_rows=2000, seed=1, start="2025-01-01"):
    rng = np.random.default_rng(seed)
    index = _grid(n_rows, start, "5min")
    return pd.DataFrame({
        "oi": 1e4 * np.exp(np.cumsum(rng.normal(0, 0.01, n_rows))),
        "top_ratio": np.exp(rng.normal(0, 0.1, n_rows)),
        "global_ratio": np.exp(rng.normal(0, 0.1, n_rows)),
        "taker_ratio": np.exp(rng.normal(0, 0.3, n_rows)),
    }, index=index)


def _depth(n_rows=6000, seed=2, start="2025-01-01"):
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=n_rows, freq="37s", tz="UTC")
    base = rng.uniform(1e6, 2e6, (n_rows, 2))
    return pd.DataFrame({"bid02": base[:, 0], "ask02": base[:, 1],
                         "bid1": base[:, 0] * 5, "ask1": base[:, 1] * 5},
                        index=index)


# --- parsing -----------------------------------------------------------------

def test_parse_metrics_drops_the_duplicated_row_and_keeps_the_used_columns():
    frame = parse_metrics(_METRICS_CSV)
    assert list(frame.columns) == ["oi", "top_ratio", "global_ratio", "taker_ratio"]
    assert len(frame) == 2
    assert frame.index[0] == pd.Timestamp("2020-09-01", tz="UTC")
    assert frame["top_ratio"].iloc[0] == pytest.approx(1.23012681)  # positions
    assert frame["global_ratio"].iloc[0] == pytest.approx(1.35731217)


def test_parse_depth_takes_the_02_and_1_percent_bands_with_bids_below_mid():
    rows = ["timestamp,percentage,depth,notional"]
    for pct, notional in ((-5, 9), (-1, 5), (-0.2, 1), (0.2, 2), (1, 7), (5, 11)):
        rows.append(f"2023-01-01 00:06:05,{pct},1.0,{notional}")
    rows.append("2023-01-01 00:06:40,-0.2,1.0,3")  # a snapshot missing bands
    frame = parse_depth("\n".join(rows))
    assert len(frame) == 1
    snap = frame.iloc[0]
    assert (snap.bid02, snap.ask02, snap.bid1, snap.ask1) == (1, 2, 5, 7)


# --- the as-of rule: only rows available by the bar's CLOSE ------------------

def _one_column(stamps):
    index = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True))
    return pd.DataFrame({"v": np.arange(1.0, len(stamps) + 1)}, index=index)


def test_a_row_available_exactly_at_the_bar_close_is_seen_by_that_bar():
    frame = _one_column(["2025-01-01 00:10", "2025-01-01 00:15"])
    got = asof(frame, frame.index, _grid(1), pd.Timedelta("15min"))
    assert got["v"].iloc[0] == 2.0  # bar 00:00 closes at 00:15


def test_a_row_one_step_after_the_bar_close_is_not_seen_by_that_bar():
    frame = _one_column(["2025-01-01 00:10", "2025-01-01 00:20"])
    got = asof(frame, frame.index, _grid(1), pd.Timedelta("15min"))
    assert got["v"].iloc[0] == 1.0


def test_a_row_older_than_the_max_age_at_the_close_is_nan_not_carried():
    frame = _one_column(["2024-12-31 23:50"])
    got = asof(frame, frame.index, _grid(1), pd.Timedelta("15min"))
    assert np.isnan(got["v"].iloc[0])


def test_the_5min_delay_hides_the_row_stamped_at_the_close():
    metrics = _metrics()
    bars = _grid(40)
    now = metrics_features(metrics, bars, pd.Timedelta(0))
    delayed = metrics_features(metrics, bars, METRICS_STEP)
    # Under the delay, bar t's feature is exactly what the row one step
    # earlier gives with no delay: shift the rows 5 min later and compare.
    shifted = metrics.copy()
    shifted.index = shifted.index + METRICS_STEP
    assert np.allclose(delayed.to_numpy(), metrics_features(
        shifted, bars, pd.Timedelta(0)).to_numpy(), equal_nan=True)
    assert not np.allclose(now.to_numpy(), delayed.to_numpy(), equal_nan=True)


@pytest.mark.parametrize("delay", [pd.Timedelta(0), METRICS_STEP])
@pytest.mark.parametrize("seed", range(5))
def test_metrics_features_never_read_a_row_available_after_the_bar_close(seed, delay):
    metrics = _metrics(seed=seed)
    bars = _grid(600)
    full = metrics_features(metrics, bars, delay)
    rng = np.random.default_rng(seed)
    for t in rng.integers(100, 600, 8):
        close = bars[t] + pd.Timedelta("15min")
        future = metrics.index + delay > close
        scrambled = metrics.copy()
        scrambled.loc[future] = scrambled.loc[future].to_numpy()[::-1] * 3.7
        got = metrics_features(scrambled, bars, delay).iloc[t]
        assert np.allclose(got.to_numpy(), full.iloc[t].to_numpy(), equal_nan=True)


@pytest.mark.parametrize("seed", range(5))
def test_depth_features_never_read_a_snapshot_taken_after_the_bar_close(seed):
    depth = _depth(seed=seed)
    bars = _grid(200)
    full = depth_features(depth, bars)
    rng = np.random.default_rng(seed)
    for t in rng.integers(10, 200, 8):
        future = depth.index > bars[t] + pd.Timedelta("15min")
        scrambled = depth.copy()
        scrambled.loc[future] = scrambled.loc[future].to_numpy()[::-1] * 2.3
        got = depth_features(scrambled, bars).iloc[t]
        assert np.allclose(got.to_numpy(), full.iloc[t].to_numpy(), equal_nan=True)


def test_feature_columns_are_the_pre_registered_ones():
    assert tuple(metrics_features(_metrics(), _grid(40), pd.Timedelta(0))
                 .columns) == METRICS_FEATURES
    assert tuple(depth_features(_depth(), _grid(40)).columns) == DEPTH_FEATURES


def test_depth_imbalance_is_positive_when_bids_outweigh_asks():
    depth = pd.DataFrame({"bid02": [3.0], "ask02": [1.0], "bid1": [1.0],
                          "ask1": [3.0]},
                         index=pd.DatetimeIndex([pd.Timestamp("2025-01-01 00:14",
                                                              tz="UTC")]))
    got = depth_features(depth, _grid(1)).iloc[0]
    assert got.depth_imb_02 == pytest.approx(0.5)
    assert got.depth_imb_1 == pytest.approx(-0.5)


# --- the stamp-convention check: it can fail both ways -----------------------

def _taker_driven_by_returns(stamp_ends_period: bool, n_bars=3000, seed=4):
    """5-min returns drive the taker ratio of the period they happen in;
    the row is stamped at the period's end or its start."""
    rng = np.random.default_rng(seed)
    r5 = rng.normal(0, 1e-3, n_bars * 3)
    period_start = _grid(n_bars * 3, freq="5min")
    stamps = period_start + (METRICS_STEP if stamp_ends_period else pd.Timedelta(0))
    metrics = pd.DataFrame({"oi": 1.0, "top_ratio": 1.0, "global_ratio": 1.0,
                            "taker_ratio": np.exp(r5 * 300)}, index=stamps)
    # bar T (open) closes at T+15: its close price after its three 5-min moves
    close = pd.Series(100 * np.exp(np.cumsum(r5))[2::3], index=_grid(n_bars))
    return metrics, close


def test_stamps_that_end_their_period_get_no_delay():
    info = check_stamp_convention(*_taker_driven_by_returns(True))
    assert info["delay"] == pd.Timedelta(0)
    assert info["corr_close_row"] > 0.3


def test_stamps_that_start_their_period_get_the_5min_delay():
    info = check_stamp_convention(*_taker_driven_by_returns(False))
    assert info["delay"] == METRICS_STEP
    assert info["corr_open_row"] > 0.3


# --- the control and the join -----------------------------------------------

def test_the_leaked_control_is_the_next_bars_log_return():
    bars = _grid(5)
    close = pd.Series([100.0, 101.0, 99.0, 99.0, 102.0], index=bars)
    feats = pd.DataFrame({"a": 1.0, "b": 2.0}, index=bars)
    leaked = leaked_control(feats, close, "b")
    assert leaked["b"].iloc[0] == pytest.approx(np.log(101 / 100))
    assert np.isnan(leaked["b"].iloc[-1])
    assert (leaked["a"] == 1.0).all()


def test_rows_without_a_complete_archive_row_are_dropped_not_filled():
    bars = _grid(3)
    X = pd.DataFrame({"f": [1.0, 2.0, 3.0]}, index=bars)
    y = pd.Series([0, 1, 0], index=bars)
    feats = pd.DataFrame({"g": [1.0, np.nan, 3.0]}, index=bars)
    X2, y2 = with_archive_features(X, y, feats)
    assert list(X2.index) == [bars[0], bars[2]]
    assert list(y2) == [0, 0]
