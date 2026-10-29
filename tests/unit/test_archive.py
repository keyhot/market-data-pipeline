import numpy as np
import pandas as pd
import pytest

from model.archive import (
    DEPTH_FEATURES,
    METRICS_FEATURES,
    METRICS_STEP,
    asof,
    check_stamp_convention,
    combine_days,
    depth_features,
    leaked_control,
    metrics_coverage,
    metrics_features,
    parse_depth,
    parse_metrics,
    row_delays,
    stamp_delays,
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
    return pd.DataFrame({"bid1": base[:, 0], "ask1": base[:, 1],
                         "bid2": base[:, 0] * 5, "ask2": base[:, 1] * 5},
                        index=index)


# --- parsing -----------------------------------------------------------------

def test_parse_metrics_drops_the_duplicated_row_and_keeps_the_used_columns():
    frame = parse_metrics(_METRICS_CSV)
    assert list(frame.columns) == ["oi", "top_ratio", "global_ratio", "taker_ratio"]
    assert len(frame) == 2
    assert frame.index[0] == pd.Timestamp("2020-09-01", tz="UTC")
    assert frame["top_ratio"].iloc[0] == pytest.approx(1.23012681)  # positions
    assert frame["global_ratio"].iloc[0] == pytest.approx(1.35731217)


def test_parse_metrics_keeps_a_row_when_only_an_unrelated_column_is_empty():
    """KI-093: 2022's rows are mostly missing sum_toptrader_long_short_ratio
    and sum_taker_long_short_vol_ratio, but a caller reading only `oi`
    (scripts/cross_section_report.py's _fetch_oi) must still see it."""
    csv = ("create_time,symbol,sum_open_interest,sum_open_interest_value,"
          "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
          "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
          "2022-02-01 00:00:00,BTCUSDT,39080.0,456144339.0,1.1,,1.35,\n")
    frame = parse_metrics(csv)
    assert len(frame) == 1
    assert frame["oi"].iloc[0] == pytest.approx(39080.0)
    assert frame["global_ratio"].iloc[0] == pytest.approx(1.35)
    assert np.isnan(frame["top_ratio"].iloc[0])
    assert np.isnan(frame["taker_ratio"].iloc[0])


def test_parse_metrics_drops_a_row_with_every_used_column_empty():
    csv = ("create_time,symbol,sum_open_interest,sum_open_interest_value,"
          "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
          "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
          "2022-02-01 00:00:00,BTCUSDT,,456144339.0,1.1,,,\n")
    assert len(parse_metrics(csv)) == 0


def test_metrics_coverage_reports_rows_and_completeness_per_year():
    idx = (pd.date_range("2022-01-01", periods=3, freq="5min", tz="UTC")
          .append(pd.date_range("2023-01-01", periods=2, freq="5min", tz="UTC")))
    frame = pd.DataFrame({
        "oi": [1.0, 2.0, 3.0, 4.0, 5.0],
        "top_ratio": [1.0, np.nan, np.nan, 1.0, 1.0],
        "global_ratio": [1.0, 1.0, 1.0, 1.0, 1.0],
        "taker_ratio": [1.0, 1.0, np.nan, 1.0, 1.0],
    }, index=idx)
    cov = metrics_coverage(frame)
    assert cov.loc[2022, "rows"] == 3
    assert cov.loc[2022, "complete"] == 1
    assert cov.loc[2022, "missing_top_ratio"] == 2
    assert cov.loc[2023, "rows"] == 2
    assert cov.loc[2023, "complete"] == 2


def test_parse_depth_takes_the_1_and_2_percent_bands_with_bids_below_mid():
    rows = ["timestamp,percentage,depth,notional"]
    for pct, notional in ((-5, 9), (-2, 5), (-1, 1), (1, 2), (2, 7), (5, 11)):
        rows.append(f"2023-01-01 00:06:05,{pct},1.0,{notional}")
    rows.append("2023-01-01 00:06:40,-1,1.0,3")  # a snapshot missing bands
    frame = parse_depth("\n".join(rows))
    assert len(frame) == 1
    snap = frame.iloc[0]
    assert (snap.bid1, snap.ask1, snap.bid2, snap.ask2) == (1, 2, 5, 7)


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
    depth = pd.DataFrame({"bid1": [3.0], "ask1": [1.0], "bid2": [1.0],
                          "ask2": [3.0]},
                         index=pd.DatetimeIndex([pd.Timestamp("2025-01-01 00:14",
                                                              tz="UTC")]))
    got = depth_features(depth, _grid(1)).iloc[0]
    assert got.depth_imb_1 == pytest.approx(0.5)
    assert got.depth_imb_2 == pytest.approx(-0.5)


# --- the stamp-convention check: it can fail both ways -----------------------

def _taker_driven_by_returns(stamp_ends_period: bool, n_bars=3000, seed=4,
                             start="2025-01-01"):
    """5-min returns drive the taker ratio of the period they happen in;
    the row is stamped at the period's end or its start."""
    rng = np.random.default_rng(seed)
    r5 = rng.normal(0, 1e-3, n_bars * 3)
    period_start = _grid(n_bars * 3, start, "5min")
    stamps = period_start + (METRICS_STEP if stamp_ends_period else pd.Timedelta(0))
    metrics = pd.DataFrame({"oi": 1.0, "top_ratio": 1.0, "global_ratio": 1.0,
                            "taker_ratio": np.exp(r5 * 300)}, index=stamps)
    # bar T (open) closes at T+15: its close price after its three 5-min moves
    close = pd.Series(100 * np.exp(np.cumsum(r5))[2::3], index=_grid(n_bars, start))
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


def test_a_convention_that_flips_between_years_gets_the_5min_delay():
    """Binance's stamps END their period through 2023 and START it from
    2024: a pooled check would let one regime's leak through."""
    m_old, c_old = _taker_driven_by_returns(True, start="2022-10-01", n_bars=43872)
    m_new, c_new = _taker_driven_by_returns(False, start="2024-01-01", seed=5)
    info = check_stamp_convention(pd.concat([m_old.iloc[:-1], m_new]),
                                  pd.concat([c_old, c_new]))
    assert all(info["by_year"][y][0] > info["by_year"][y][1] for y in (2022, 2023))
    assert info["by_year"][2024][0] < info["by_year"][2024][1]
    assert info["delay"] == METRICS_STEP  # the frozen whole-sample scalar
    # KI-092: each year keeps its own verdict instead
    assert info["delays"] == {2022: pd.Timedelta(0), 2023: pd.Timedelta(0),
                              2024: METRICS_STEP}


def test_a_zero_open_interest_row_is_skipped_not_logged_to_minus_infinity():
    metrics = _metrics()
    metrics.iloc[480, metrics.columns.get_loc("oi")] = 0.0  # bar 159's close
    feats = metrics_features(metrics, _grid(160), pd.Timedelta(0))
    assert np.isfinite(feats.iloc[-1].to_numpy()).all()


# --- KI-092: per-regime delay across the 2024 stamp flip --------------------

_SEAM = pd.Timestamp("2024-01-01", tz="UTC")
_FLIP = {2023: pd.Timedelta(0), 2024: METRICS_STEP}  # A2's real-archive map
_UNIT = 1e-4  # log value per 5-min step of a row's period END


def _day_file(day, stamp_ends_period):
    """One synthetic daily metrics file. Every column's log encodes the END
    of the 5-min period the row covers, in steps from the seam."""
    period_start = pd.date_range(pd.Timestamp(day, tz="UTC"), periods=288,
                                 freq="5min")
    stamps = period_start + (METRICS_STEP if stamp_ends_period else pd.Timedelta(0))
    k = ((period_start + METRICS_STEP - _SEAM) / METRICS_STEP).to_numpy()
    value = np.exp(k * _UNIT)
    return pd.DataFrame({"oi": value, "top_ratio": value, "global_ratio": value,
                         "taker_ratio": value}, index=stamps)


def _flipped_archive():
    """Stamps END their period through 2023, START it from 2024; the 12-31
    file's last stamp and the 01-01 file's first are both 2024-01-01 00:00."""
    return combine_days([_day_file("2023-12-30", True),
                         _day_file("2023-12-31", True),
                         _day_file("2024-01-01", False),
                         _day_file("2024-01-02", False)])


def test_the_seam_stamp_keeps_the_later_files_row():
    merged = _flipped_archive()
    assert merged.index.is_unique and merged.index.is_monotonic_increasing
    k = np.log(merged.loc[_SEAM, "taker_ratio"]) / _UNIT
    assert k == pytest.approx(1)  # the 2024 row: period [00:00, 00:05)


def test_the_as_run_window_is_the_bars_own_on_both_sides_of_a_stamp_flip():
    """KI-092. With per-regime delays every bar reads the row whose period
    ends exactly at its close, and a taker window of exactly its own 15 min,
    on both sides of the flip. The one bar closing ON the seam lost its true
    row to the dedup: it is 5 min stale, never early."""
    bars = pd.date_range("2023-12-30 06:00", "2024-01-02 18:00", freq="15min",
                         tz="UTC")
    feats = metrics_features(_flipped_archive(), bars, _FLIP)
    close = bars + pd.Timedelta("15min")
    want = ((close - _SEAM) / METRICS_STEP).to_numpy()  # period end = close
    row = feats.top_ls.to_numpy() / _UNIT
    window = feats.taker_ls.to_numpy() / _UNIT  # mean period end of the window
    seam = close == _SEAM
    assert seam.sum() == 1 and (close < _SEAM).sum() > 50 and (close > _SEAM).sum() > 50
    assert np.allclose(row[~seam], want[~seam], rtol=0, atol=1e-6)
    assert np.allclose(window[~seam], want[~seam] - 1, rtol=0, atol=1e-6)
    assert row[seam][0] == pytest.approx(-1)  # ends 23:55: stale, not early
    assert window[seam][0] == pytest.approx(-2)


def test_a_fallback_row_after_the_seam_windows_only_periods_ending_after_it():
    """With the 00:10 row missing, the bar closing 00:15 falls back to the
    00:05 row. Its taker window is the periods ENDING in (23:55, 00:10] --
    the 2024 rows 00:00 and 00:05 -- not the stamps in (23:50, 00:05], which
    would pull in the 2023 period ending 23:55 (agy review, verified)."""
    metrics = _flipped_archive().drop(_SEAM + 2 * METRICS_STEP)
    feats = metrics_features(metrics, pd.DatetimeIndex([_SEAM], tz="UTC"), _FLIP)
    assert feats.top_ls.iloc[0] / _UNIT == pytest.approx(2)  # the 00:05 row
    assert feats.taker_ls.iloc[0] / _UNIT == pytest.approx(1.5)


@pytest.mark.parametrize("delay", [pd.Timedelta(0), METRICS_STEP])
def test_a_year_map_that_agrees_everywhere_is_exactly_the_scalar_delay(delay):
    metrics = _metrics(n_rows=4000, start="2023-12-25")
    bars = _grid(1200, start="2023-12-25")
    assert set(metrics.index.year) == {2023, 2024}
    scalar = metrics_features(metrics, bars, delay)
    mapped = metrics_features(metrics, bars, {2023: delay, 2024: delay})
    assert np.array_equal(scalar.to_numpy(), mapped.to_numpy(), equal_nan=True)


@pytest.mark.parametrize("seed", range(3))
def test_per_year_delays_never_read_a_row_available_after_the_bar_close(seed):
    metrics = _metrics(n_rows=4000, seed=seed, start="2023-12-25")
    bars = _grid(1200, start="2023-12-25")
    full = metrics_features(metrics, bars, _FLIP)
    available = metrics.index + row_delays(metrics.index, _FLIP)
    rng = np.random.default_rng(seed)
    # bar 671 closes on the seam
    for t in [670, 671, 672, 673, *rng.integers(100, 1200, 6)]:
        future = available > bars[t] + pd.Timedelta("15min")
        scrambled = metrics.copy()
        scrambled.loc[future] = scrambled.loc[future].to_numpy()[::-1] * 3.7
        got = metrics_features(scrambled, bars, _FLIP).iloc[t]
        assert np.allclose(got.to_numpy(), full.iloc[t].to_numpy(), equal_nan=True)


def test_each_year_keeps_its_own_verdict_and_a_year_without_one_gets_5min():
    assert stamp_delays({2023: (0.273, -0.020), 2024: (0.026, 0.255),
                         2025: (np.nan, np.nan)}) == {
        2023: pd.Timedelta(0), 2024: METRICS_STEP, 2025: METRICS_STEP}
    stamps = pd.DatetimeIndex(["2023-12-31 23:55", "2024-01-01 00:00",
                               "2022-06-01"], tz="UTC")
    assert list(row_delays(stamps, _FLIP)) == [pd.Timedelta(0), METRICS_STEP,
                                               METRICS_STEP]


def test_availability_that_runs_backwards_is_refused_not_mis_joined():
    metrics = pd.DataFrame(1.0, columns=["oi", "top_ratio", "global_ratio",
                                         "taker_ratio"],
                           index=pd.DatetimeIndex(["2023-12-31 23:58",
                                                   "2024-01-01 00:01"], tz="UTC"))
    with pytest.raises(ValueError, match="availability"):
        metrics_features(metrics, _grid(2, start="2024-01-01"),
                         {2023: METRICS_STEP, 2024: pd.Timedelta(0)})
