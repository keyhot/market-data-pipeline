import numpy as np
import pandas as pd
import pytest

from model.funding import (
    FUNDING_FEATURES,
    check_grid,
    funding_features,
    funding_series,
    kline_close,
    leaked_control,
    with_funding_features,
)

_15M = 900_000
_8H = 8 * 3_600_000
# A real BTCUSDT settlement, verbatim from /fapi/v1/fundingRate (2020-01-01).
_LIVE_FUNDING = [[1577836800000, "-0.00012359"], [1577865600000, "-0.00012383"]]


def _ms(stamp: str) -> int:
    return int(pd.Timestamp(stamp, tz="UTC").timestamp() * 1000)


def _klines(closes, start="2026-01-01"):
    t0 = _ms(start)
    return [[t0 + i * _15M, c, c, c, c, "0", t0 + (i + 1) * _15M - 1, "0", 15,
             "0", "0", "0"] for i, c in enumerate(closes)]


def _grid(n, start="2026-01-01"):
    return pd.date_range(start, periods=n, freq="15min", tz="UTC")


def _premium(n=200, seed=3, start="2026-01-01"):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0, 3e-4, n), index=_grid(n, start))


# --- the as-of rule: a bar sees a settlement only once the bar has CLOSED ----

def test_a_settlement_47ms_after_the_hour_is_first_seen_by_the_bar_closing_after_it():
    funding = funding_series([[_ms("2026-01-01 00:00"), "0.0001"],
                              [_ms("2026-01-01 08:00") + 47, "0.0005"]])
    feats = funding_features(funding, _premium(), _grid(200))

    closes_at_0800 = pd.Timestamp("2026-01-01 07:45", tz="UTC")
    closes_at_0815 = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    assert feats.loc[closes_at_0800, "funding_last"] == pytest.approx(0.0)
    assert feats.loc[closes_at_0815, "funding_last"] == pytest.approx(0.0004)


def test_a_settlement_exactly_at_the_bar_close_is_seen_by_that_bar():
    funding = funding_series([[_ms("2026-01-01 00:00"), "0.0001"],
                              [_ms("2026-01-01 08:00"), "0.0003"]])
    feats = funding_features(funding, _premium(), _grid(200))

    assert feats.loc[pd.Timestamp("2026-01-01 07:45", tz="UTC"),
                     "funding_last"] == pytest.approx(0.0002)
    assert feats.loc[pd.Timestamp("2026-01-01 07:30", tz="UTC"),
                     "funding_last"] == pytest.approx(0.0)


def test_funding_change_is_the_step_from_the_previous_settlement():
    funding = funding_series([[_ms("2026-01-01 00:00"), "0.0001"],
                              [_ms("2026-01-01 08:00"), "0.0004"],
                              [_ms("2026-01-01 16:00"), "-0.0002"]])
    feats = funding_features(funding, _premium(), _grid(200))

    at = lambda s: feats.loc[pd.Timestamp(s, tz="UTC")]  # noqa: E731
    assert at("2026-01-01 12:00").funding_change == pytest.approx(0.0003)
    assert at("2026-01-01 20:00").funding_change == pytest.approx(-0.0006)
    assert at("2026-01-01 20:00").funding_last == pytest.approx(-0.0003)


def test_bars_before_the_first_settlement_have_no_funding_row():
    funding = funding_series([[_ms("2026-01-01 08:00"), "0.0001"]])
    feats = funding_features(funding, _premium(), _grid(200))

    assert feats.loc[:"2026-01-01 07:30", "funding_last"].isna().all()


# --- premium features read bars <= t only -------------------------------------

def test_premium_features_are_the_pre_registered_windows():
    premium = _premium()
    feats = funding_features(funding_series(_LIVE_FUNDING), premium, _grid(200))

    t = premium.index[100]
    assert feats.loc[t, "premium_1"] == premium.loc[t]
    assert feats.loc[t, "premium_mean_32"] == pytest.approx(
        premium.iloc[69:101].mean())
    assert feats.loc[t, "premium_change_4"] == pytest.approx(
        premium.iloc[100] - premium.iloc[96])


def test_no_feature_at_t_changes_when_the_future_is_removed():
    premium = _premium(400)
    funding = funding_series([[_ms("2026-01-01") + i * _8H, f"{0.0001 * (i % 3)}"]
                              for i in range(5)])
    full = funding_features(funding, premium, _grid(400))
    for cut in (150, 250, 399):
        t = premium.index[cut]
        close = t + pd.Timedelta(minutes=15)
        past_funding = funding[funding.index <= close]
        truncated = funding_features(past_funding, premium.iloc[:cut + 1],
                                     _grid(cut + 1))
        pd.testing.assert_series_equal(truncated.loc[t], full.loc[t])


def test_all_five_features_are_produced():
    feats = funding_features(funding_series(_LIVE_FUNDING), _premium(), _grid(200))
    assert tuple(feats.columns) == FUNDING_FEATURES


# --- integrity: a wrong read fails loudly -------------------------------------

def test_reads_the_verified_funding_rows():
    series = funding_series(_LIVE_FUNDING)
    assert series.index[0] == pd.Timestamp("2020-01-01", tz="UTC")
    assert series.iloc[1] == -0.00012383


def test_funding_out_of_order_is_refused():
    with pytest.raises(ValueError, match="increasing"):
        funding_series(list(reversed(_LIVE_FUNDING)))


def test_funding_off_the_8h_schedule_is_refused():
    with pytest.raises(ValueError, match="8h"):
        funding_series([[_ms("2026-01-01 00:00"), "0.0001"],
                        [_ms("2026-01-01 04:00"), "0.0001"]])


def test_kline_close_is_indexed_by_open_time():
    close = kline_close(_klines([1.0, 2.0]))
    assert close.index[1] == pd.Timestamp("2026-01-01 00:15", tz="UTC")
    assert close.iloc[1] == 2.0


def test_a_kline_that_is_not_15m_wide_is_refused():
    rows = _klines([1.0, 2.0])
    rows[1][6] += _15M
    with pytest.raises(ValueError, match="15m"):
        kline_close(rows)


def test_duplicate_klines_are_refused():
    rows = _klines([1.0, 2.0])
    with pytest.raises(ValueError, match="duplicate"):
        kline_close(rows + rows[1:])


# --- the grid check that can fail -----------------------------------------------

def _walk(n=2_000, seed=5):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.003, n))),
                     index=_grid(n))


def test_an_index_on_the_spot_grid_passes():
    spot = _walk()
    index = spot * (1 + np.random.default_rng(9).normal(0, 5e-5, len(spot)))
    medians = check_grid(index, spot)
    assert medians[0] < medians[-1] and medians[0] < medians[1]


@pytest.mark.parametrize("shift", [1, -1])
def test_an_index_one_bar_off_the_spot_grid_is_refused(shift):
    spot = _walk()
    with pytest.raises(ValueError, match="misaligned"):
        check_grid(spot.shift(shift).dropna(), spot)


def test_a_small_but_consistent_basis_still_passes_under_3_bps():
    spot = _walk()
    check_grid(spot * 1.0002, spot)
    with pytest.raises(ValueError, match="misaligned"):
        check_grid(spot * 1.0004, spot)


# --- joins and the control ------------------------------------------------------

def test_rows_without_a_full_feature_row_are_dropped_never_filled():
    grid = _grid(200)
    X = pd.DataFrame({"f": np.arange(200.0)}, index=grid)
    y = pd.Series(np.arange(200) % 2, index=grid)
    # funding_change needs two settlements seen; the first arrives at 08:00
    # (08:00:00.047), so the first full row is the bar closing at 08:15
    feats = funding_features(funding_series([[_ms("2026-01-01 00:00") + 47, "0.0001"],
                                             [_ms("2026-01-01 08:00") + 47, "0.0002"]]),
                             _premium(), grid)

    X2, y2 = with_funding_features(X, y, feats)
    assert not X2.isna().any().any()
    assert X2.index.equals(y2.index)
    assert X2.index[0] == pd.Timestamp("2026-01-01 08:00", tz="UTC")
    assert set(FUNDING_FEATURES) <= set(X2.columns)


def test_the_control_leaks_the_next_bars_index_return():
    grid = _grid(200)
    index = pd.Series(np.linspace(100, 120, 200), index=grid)
    feats = funding_features(funding_series(_LIVE_FUNDING), _premium(), grid)

    leaked = leaked_control(feats, index)
    t = grid[50]
    assert leaked.loc[t, "premium_change_4"] == pytest.approx(
        np.log(index.iloc[51] / index.iloc[50]))
    assert np.isnan(leaked["premium_change_4"].iloc[-1])
    pd.testing.assert_frame_equal(leaked.drop(columns="premium_change_4"),
                                  feats.drop(columns="premium_change_4"))


def test_a_series_too_quiet_to_tell_the_lags_apart_is_refused():
    # lag 0 is under 3 bps, but so are its neighbours: alignment is unproven
    rng = np.random.default_rng(11)
    spot = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 1e-4, 2_000))),
                     index=_grid(2_000))
    index = spot * (1 + rng.normal(0, 1e-4, len(spot)))
    with pytest.raises(ValueError, match="misaligned"):
        check_grid(index, spot)
