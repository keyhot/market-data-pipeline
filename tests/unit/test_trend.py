"""Unit tests for `model/trend.py` (E16 trend, pre-registered in the vault
`Docs/trend-experiment.md`). Test-first per the note's implementation spec.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model import trend
from model.backtest import COST_PROFILES
from model.benchmark import exposure_matched_return


def _ts(s):
    return pd.Timestamp(s, tz="UTC")


def _daily_index(start, n):
    return pd.date_range(_ts(start), periods=n, freq="D", tz="UTC")


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

def test_cost_side_constants_pinned():
    assert trend.COST_SIDE_PERP == pytest.approx(0.0006)
    assert trend.COST_SIDE_SPOT == pytest.approx(0.0011)
    assert trend.COST_SIDE_PERP == pytest.approx(
        COST_PROFILES["perp_taker"].fee_per_side + COST_PROFILES["perp_taker"].slippage
    )


# --------------------------------------------------------------------------
# Monthly grid
# --------------------------------------------------------------------------

def test_month_end_closes_picks_close_at_month_start():
    idx = _daily_index("2020-01-01", 400)
    close = pd.Series(np.arange(1.0, len(idx) + 1), index=idx)
    out = trend.month_end_closes(close)
    assert out.index.freqstr in ("MS", None)
    # 2020-02-01 is day 32 (index 31) of the close-time series -> value 32.0
    assert out.loc[_ts("2020-02-01")] == pytest.approx(32.0)


def test_month_end_closes_nan_before_series_starts():
    idx = _daily_index("2020-01-15", 60)
    close = pd.Series(1.0, index=idx)
    out = trend.month_end_closes(close)
    assert pd.isna(out.loc[_ts("2020-01-01")])  # before the series starts
    assert not pd.isna(out.loc[_ts("2020-02-01")])


def test_monthly_returns_first_row_nan_second_row_ratio():
    idx = pd.date_range(_ts("2020-01-01"), periods=4, freq="MS", tz="UTC")
    closes = pd.DataFrame({"A": [100.0, 110.0, 121.0, 90.0]}, index=idx)
    r = trend.monthly_returns(closes)
    assert pd.isna(r["A"].iloc[0])
    assert r["A"].iloc[1] == pytest.approx(0.10)
    assert r["A"].iloc[2] == pytest.approx(0.10)
    assert r["A"].iloc[3] == pytest.approx(90.0 / 121.0 - 1.0)


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

def _month_closes_fixture():
    idx = pd.date_range(_ts("2019-01-01"), periods=14, freq="MS", tz="UTC")
    # A rises steadily; B falls steadily; index 13 = 2020-02-01
    a = 100.0 * (1.02 ** np.arange(14))
    b = 100.0 * (0.98 ** np.arange(14))
    return pd.DataFrame({"A": a, "B": b}, index=idx)


def test_signals_hand_built_signs():
    mc = _month_closes_fixture()
    T = mc.index[-1]  # 2020-02-01
    sig = trend.signals(mc, T)
    assert (sig.loc["A"] == 1.0).all()   # steadily rising -> always positive
    assert (sig.loc["B"] == -1.0).all()  # steadily falling -> always negative


def test_signals_exact_zero_return_is_plus_one():
    idx = pd.date_range(_ts("2019-01-01"), periods=14, freq="MS", tz="UTC")
    a = np.full(14, 100.0)  # flat -> every lookback return is exactly 0
    mc = pd.DataFrame({"A": a}, index=idx)
    sig = trend.signals(mc, mc.index[-1])
    assert (sig.loc["A"] == 1.0).all()


def test_signals_truncation_property():
    mc = _month_closes_fixture()
    rng = np.random.default_rng(3)
    candidates = mc.index[12:]  # need >=12 months history for k=12
    for T in rng.choice(candidates, size=min(2, len(candidates)), replace=False):
        full = trend.signals(mc, T)
        truncated = trend.signals(mc.loc[mc.index <= T], T)
        pd.testing.assert_frame_equal(full, truncated)


# --------------------------------------------------------------------------
# MOP vol
# --------------------------------------------------------------------------

def test_mop_vol_matches_explicit_weighted_sum():
    idx = _daily_index("2020-01-01", 11)
    rng = np.random.default_rng(1)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 11)), index=idx)
    T = idx[-1]
    got = trend.mop_vol(close, T, annual_days=365)

    r = close.pct_change().dropna().to_numpy()  # 10 returns
    delta = 60.0 / 61.0
    n = len(r)
    # pandas ewm(adjust=True): weight of the i-th-from-the-end observation
    # is delta**i (i=0 = most recent).
    w = delta ** np.arange(n - 1, -1, -1)
    rbar = np.sum(w * r) / np.sum(w)
    v = np.sum(w * (r - rbar) ** 2) / np.sum(w)
    expected = np.sqrt(365 * v)
    assert got == pytest.approx(expected, rel=1e-9)


def test_mop_vol_constant_series_is_zero():
    idx = _daily_index("2020-01-01", 20)
    close = pd.Series(100.0, index=idx)
    assert trend.mop_vol(close, idx[-1]) == pytest.approx(0.0, abs=1e-12)


def test_mop_vol_truncates_internally():
    idx = _daily_index("2020-01-01", 40)
    rng = np.random.default_rng(2)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 40)), index=idx)
    T = idx[19]
    full = trend.mop_vol(close, T)
    truncated = trend.mop_vol(close.loc[close.index <= T], T)
    assert full == pytest.approx(truncated, rel=1e-12)


# --------------------------------------------------------------------------
# HOP scaling
# --------------------------------------------------------------------------

def test_hop_target_vol_hit_exactly():
    S = pd.Series({"A": 1.0, "B": 1.0})
    sigma = pd.Series({"A": 0.5, "B": 0.6})
    cov = np.diag([0.02, 0.03])
    e, capped = trend.hop_exposures(S, sigma, cov, target=0.10, cap=100.0)
    realized = np.sqrt(12 * (e.to_numpy() @ cov @ e.to_numpy()))
    assert realized == pytest.approx(0.10, rel=1e-9)
    assert not capped


def test_hop_invariant_to_annualization_and_scale_constants():
    S = pd.Series({"A": 1.0, "B": -1.0})
    sigma = pd.Series({"A": 0.5, "B": 0.6})
    cov = np.array([[0.02, 0.005], [0.005, 0.03]])
    e_365, _ = trend.hop_exposures(S, sigma, cov)
    # scaling both sigmas by the same constant (365 -> 261 style change) must cancel
    e_261, _ = trend.hop_exposures(S, sigma * np.sqrt(261 / 365), cov)
    np.testing.assert_allclose(e_365.to_numpy(), e_261.to_numpy(), rtol=1e-12)
    # MOP's dropped 40% constant on W must also cancel
    e_scaled, _ = trend.hop_exposures(S, sigma / 0.4, cov)
    np.testing.assert_allclose(e_365.to_numpy(), e_scaled.to_numpy(), rtol=1e-12)


def test_hop_cap_binds_on_correlated_hedge_uncapped_at_zero_correlation():
    S = pd.Series({"A": 1.0, "B": -1.0})
    sigma = pd.Series({"A": 1.0, "B": 1.0})
    var = 0.09  # a long-A/short-B hedge on correlated assets has LOW
                # portfolio variance, so lambda must scale up hard to hit
                # the 10% vol target -- picked so 0.99 correlation binds
                # the cap and 0 correlation does not.
    cov_corr = np.array([[var, 0.99 * var], [0.99 * var, var]])
    e, capped = trend.hop_exposures(S, sigma, cov_corr, target=0.10, cap=1.0)
    assert capped
    assert e.abs().sum() == pytest.approx(1.0, rel=1e-9)

    cov_indep = np.array([[var, 0.0], [0.0, var]])
    e2, capped2 = trend.hop_exposures(S, sigma, cov_indep, target=0.10, cap=1.0)
    assert not capped2


def test_hop_wtcovw_non_positive_raises():
    S = pd.Series({"A": 1.0})
    sigma = pd.Series({"A": 1.0})
    with pytest.raises(ValueError):
        trend.hop_exposures(S, sigma, np.array([[0.0]]))


def test_hop_single_asset_collapse_demo():
    for S_val, sigma_m in [(1.0, 0.4), (-1.0, 0.7), (0.5, 0.2)]:
        S = pd.Series({"A": S_val})
        sigma = pd.Series({"A": sigma_m})
        cov = np.array([[sigma_m ** 2]])  # 1x1 "covariance" = the vol itself squared
        e, _ = trend.hop_exposures(S, sigma, cov, target=0.10, cap=100.0)
        expected = np.sign(S_val) * 0.10 / (sigma_m * np.sqrt(12))
        assert e["A"] == pytest.approx(expected, rel=1e-9)


def test_hop_symbol_order_asymmetric_variances():
    """A BTC/ETH swap must NOT pass silently: with asymmetric variances the
    result differs under a mislabeled ordering."""
    S = pd.Series({"BTCUSDT": 1.0, "ETHUSDT": -1.0})
    sigma = pd.Series({"BTCUSDT": 0.3, "ETHUSDT": 0.9})
    cov = np.array([[0.01, 0.002], [0.002, 0.05]])
    e, _ = trend.hop_exposures(S, sigma, cov)
    # swapping the order of S/sigma without swapping cov changes the answer
    S_swapped = pd.Series({"ETHUSDT": -1.0, "BTCUSDT": 1.0})
    sigma_swapped = pd.Series({"ETHUSDT": 0.9, "BTCUSDT": 0.3})
    e_bad, _ = trend.hop_exposures(S_swapped, sigma_swapped, cov)
    assert not np.allclose(e.reindex(["BTCUSDT", "ETHUSDT"]).to_numpy(),
                            e_bad.reindex(["BTCUSDT", "ETHUSDT"]).to_numpy())


# --------------------------------------------------------------------------
# rebalance_exposures (integration of signals/mop_vol/hop_exposures)
# --------------------------------------------------------------------------

def _spot_daily_fixture(months=48, seed=5, corr=0.5):
    """`months` calendar months of daily close-time data for two symbols,
    enough for a 36-month covariance window plus a 12-month lookback."""
    rng = np.random.default_rng(seed)
    idx = _daily_index("2017-01-01", months * 30)
    z1 = rng.normal(0, 1, len(idx))
    z2 = rng.normal(0, 1, len(idx))
    r_a = 0.0002 + 0.01 * z1
    r_b = 0.0002 + 0.01 * (corr * z1 + np.sqrt(1 - corr ** 2) * z2)
    a = 100 * np.cumprod(1 + r_a)
    b = 50 * np.cumprod(1 + r_b)
    return pd.DataFrame({"BTCUSDT": a, "ETHUSDT": b}, index=idx)


def test_rebalance_exposures_plus_one_uses_signal_one():
    spot = _spot_daily_fixture()
    T = spot.index[-30]
    T = pd.Timestamp(T.year, T.month, 1, tz="UTC")
    e, capped = trend.rebalance_exposures(spot, T, signal="plus_one")
    sigma = pd.Series({s: trend.mop_vol(spot[s], T) for s in spot.columns})
    # sign of e must match sign of 1/sigma (always positive) up to the shared lambda
    assert (np.sign(e) == 1.0).all() or (np.sign(e) == np.sign(1.0 / sigma)).all()


def test_rebalance_exposures_covariance_window_too_short_raises():
    spot = _spot_daily_fixture(months=20)  # not enough for a 36-month window
    T = pd.Timestamp(spot.index[-1].year, spot.index[-1].month, 1, tz="UTC")
    with pytest.raises(ValueError):
        trend.rebalance_exposures(spot, T, signal="plus_one")


def test_rebalance_exposures_truncation_invariance():
    spot = _spot_daily_fixture(months=48, seed=11)
    T = _ts("2017-01-01") + pd.DateOffset(months=37)  # 36 full monthly returns of runway
    full_e, full_c = trend.rebalance_exposures(spot, T, signal="trend")
    truncated = spot.loc[spot.index <= T]
    trunc_e, trunc_c = trend.rebalance_exposures(truncated, T, signal="trend")
    np.testing.assert_array_equal(full_e.to_numpy(), trunc_e.to_numpy())
    assert full_c == trunc_c


def test_rebalance_exposures_override_s():
    spot = _spot_daily_fixture()
    T = _ts("2017-01-01") + pd.DateOffset(months=37)
    override = pd.Series({"BTCUSDT": -1.0, "ETHUSDT": 1.0})
    e, _ = trend.rebalance_exposures(spot, T, override_S=override)
    plus_e, _ = trend.rebalance_exposures(spot, T, signal="plus_one")
    # BTC's exposure sign flips vs the +1 book, ETH's does not
    assert np.sign(e["BTCUSDT"]) != np.sign(plus_e["BTCUSDT"])
    assert np.sign(e["ETHUSDT"]) == np.sign(plus_e["ETHUSDT"])


# --------------------------------------------------------------------------
# Returns and funding
# --------------------------------------------------------------------------

def test_funding_boundary_1ms_after_midnight_belongs_to_previous_day():
    from model.cross_section import funding_by_day
    events = pd.DataFrame({
        "symbol": ["A", "A"],
        "time": [pd.Timestamp("2021-03-01 00:00:00.004", tz="UTC"),
                 pd.Timestamp("2021-03-01 00:01:30", tz="UTC")],
        "rate": [0.001, 0.002],
    })
    days = pd.date_range("2021-02-27", "2021-03-02", freq="D", tz="UTC")
    out = funding_by_day(events, days)
    assert out.loc[pd.Timestamp("2021-02-28", tz="UTC"), "A"] == pytest.approx(0.001)
    assert out.loc[pd.Timestamp("2021-03-01", tz="UTC"), "A"] == pytest.approx(0.002)


def test_holding_excess_returns_short_with_positive_funding():
    idx = pd.date_range(_ts("2020-01-02"), periods=90, freq="D", tz="UTC")
    close = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    perp = pd.DataFrame({"A": close})
    days = pd.date_range(idx.min().normalize(), idx.max().normalize(), freq="D", tz="UTC")
    funding_daily = pd.DataFrame({"A": 0.0001}, index=days)
    out = trend.holding_excess_returns(perp, funding_daily)
    # a long pays positive funding: r~ = price_ret - funding, funding > 0 -> r~ < price_ret
    assert (out["A"].dropna() < trend.monthly_returns(
        pd.DataFrame({"A": trend.month_end_closes(close)})
    )["A"].reindex(out.index).dropna()).all()


def test_book_returns_first_month_charged_full_entry_from_flat():
    idx = pd.date_range(_ts("2020-09-01"), periods=1, freq="MS", tz="UTC")
    e = pd.DataFrame({"A": [0.5], "B": [-0.3]}, index=idx)
    r_tilde = pd.DataFrame({"A": [0.02], "B": [0.01]}, index=idx)
    price_ret = pd.DataFrame({"A": [0.02], "B": [0.01]}, index=idx)
    out = trend.book_returns(e, r_tilde, price_ret, cost_side=0.0006)
    expected_kappa = 0.0006 * (0.5 + 0.3)
    assert out["kappa"].iloc[0] == pytest.approx(expected_kappa)


def test_book_returns_drift_matches_hand_computation():
    idx = pd.date_range(_ts("2020-09-01"), periods=2, freq="MS", tz="UTC")
    e = pd.DataFrame({"A": [1.0, 1.0]}, index=idx)
    r_tilde = pd.DataFrame({"A": [0.10, 0.00]}, index=idx)
    price_ret = pd.DataFrame({"A": [0.10, 0.00]}, index=idx)  # no funding: price_ret == r_tilde
    out = trend.book_returns(e, r_tilde, price_ret, cost_side=0.0)
    # month 1: flat -> full entry of 1.0, no cost (cost_side=0), R = G = 0.10
    assert out["R"].iloc[0] == pytest.approx(0.10)
    # month 2 drift: e_drift = 1.0 * (1.10) / (1.10) = 1.0 -> zero turnover
    assert out["kappa"].iloc[1] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Null / conditions
# --------------------------------------------------------------------------

def test_excess_over_null_constant_exposure_is_zero():
    idx = pd.date_range(_ts("2020-09-01"), periods=5, freq="MS", tz="UTC")
    e = pd.DataFrame({"A": [0.3] * 5, "B": [-0.2] * 5}, index=idx)
    r_tilde = pd.DataFrame({"A": np.random.default_rng(0).normal(0, 0.05, 5),
                            "B": np.random.default_rng(1).normal(0, 0.05, 5)}, index=idx)
    x = trend.excess_over_null(e, r_tilde)
    assert np.max(np.abs(x.to_numpy())) <= 1e-15


def test_signed_null_charged_exactly_books_own_kappa():
    idx = pd.date_range(_ts("2020-09-01"), periods=4, freq="MS", tz="UTC")
    e = pd.DataFrame({"A": [0.5, -0.3, 0.2, 0.1]}, index=idx)
    r_tilde = pd.DataFrame({"A": [0.02, -0.01, 0.03, 0.0]}, index=idx)
    price_ret = r_tilde.copy()
    br = trend.book_returns(e, r_tilde, price_ret, cost_side=0.0006)
    n, e_bar = trend.signed_null(e, r_tilde, br["kappa"])
    x_direct = trend.excess_over_null(e, r_tilde)
    x_via_r_minus_n = br["R"] - n
    pd.testing.assert_series_equal(x_direct.sort_index(), x_via_r_minus_n.sort_index(),
                                    check_names=False)


def test_conditions_ki094_uses_null_for_condition1_control_for_condition2():
    idx = pd.date_range(_ts("2020-09-01"), periods=3, freq="MS", tz="UTC")
    R_c = pd.Series([0.05, 0.02, -0.01], index=idx)
    N_c = pd.Series([0.01, 0.01, 0.01], index=idx)     # x = R_c - N_c can be > 0
    R_plus = pd.Series([0.08, 0.08, 0.08], index=idx)  # d = R_c - R_plus is < 0
    x, d = trend.conditions(R_c, N_c, R_plus)
    assert (x > 0).sum() >= 1
    assert (d < 0).all()
    pd.testing.assert_series_equal(x, R_c - N_c, check_names=False)
    pd.testing.assert_series_equal(d, R_c - R_plus, check_names=False)


def test_signed_exposure_matched_return_bitwise_parity_nonneg_exposure():
    for exposure in (0.0, 0.3, 1.0, 2.5):
        for br in (-0.05, 0.0, 0.1):
            for positions in (0, 1, 5, 12):
                for cost in (0.0, 0.0006, 0.0011):
                    a = trend.signed_exposure_matched_return(exposure, br, positions, cost)
                    b = exposure_matched_return(exposure, br, positions, cost)
                    assert a == b


def test_signed_exposure_matched_return_negative_exposure_hand_check():
    got = trend.signed_exposure_matched_return(-0.5, 0.10, 2, 0.001)
    expected = (1 + (-0.5) * 0.10) * (1 - 0.001) ** 2 - 1
    assert got == pytest.approx(expected)


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

def test_bootstrap_constant_series_ci_collapses_to_point():
    x = np.full(40, 0.02)
    point, lo, hi = trend.block_bootstrap_ci(x)
    assert point == pytest.approx(0.02)
    assert lo == pytest.approx(0.02)
    assert hi == pytest.approx(0.02)


def test_bootstrap_reproducible_under_seed():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, 64)
    a = trend.block_bootstrap_ci(x, seed=7)
    b = trend.block_bootstrap_ci(x, seed=7)
    assert a == b


def test_bootstrap_coverage_iid_normal():
    covered = 0
    trials = 200
    for seed in range(trials):
        rng = np.random.default_rng(1000 + seed)
        x = rng.normal(0, 1, 64)
        _, lo, hi = trend.block_bootstrap_ci(x, reps=500, seed=seed)
        if lo <= 0.0 <= hi:
            covered += 1
    rate = covered / trials
    assert 0.90 <= rate <= 0.98


def test_bootstrap_wraps_circularly():
    # a series where the tail (near the wrap point) differs sharply --
    # a non-circular implementation could never draw a block starting in
    # the last `block-1` positions that includes the wrapped-around head.
    x = np.array([100.0] + [0.0] * 9 + [-100.0])
    point, lo, hi = trend.block_bootstrap_ci(x, block=6, reps=2000, seed=7)
    assert lo < point < hi or lo == hi  # exercised without crashing/degenerating
    assert np.isfinite(lo) and np.isfinite(hi)


# --------------------------------------------------------------------------
# Verdict table
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mean_x,mean_d,expected", [
    (1.0, 1.0, "PASS"),
    (1.0, -1.0, "FAIL"),
    (-1.0, 1.0, "FAIL"),
    (-1.0, -1.0, "FAIL"),
    (0.0, 1.0, "FAIL"),
    (1e-12, 1.0, "PASS"),
    (1.0, 0.0, "FAIL"),
    (1.0, 1e-12, "PASS"),
])
def test_primary_verdict_table(mean_x, mean_d, expected):
    assert trend.primary_verdict(mean_x, mean_d) == expected


def test_pass_95_false_when_either_lo_non_positive():
    assert trend.pass_95(0.1, 0.1) is True
    assert trend.pass_95(0.0, 0.1) is False
    assert trend.pass_95(0.1, -0.1) is False
    assert trend.pass_95(-0.1, -0.1) is False


def test_stage_a_valid_returns_every_failing_id():
    valid, failing = trend.stage_a_valid({"a1": True, "a2": False, "a3": False})
    assert not valid
    assert set(failing) == {"a2", "a3"}


# --------------------------------------------------------------------------
# Synthetic end-to-end (the control that MUST differ), seeds 0-9
# --------------------------------------------------------------------------

def _synthetic_run(seed, mu_fn, years=8, warmup_years=3, sigma_m=0.20, rho=0.5):
    """Two synthetic assets built exactly per the frozen note's synthetic
    spec: each month's return is spread over 30 days as iid
    N(mu_t/30, sigma_m^2/30), correlation 0.5 via a shared factor. `mu_fn`
    takes the asset's OWN trailing-12-month return (as of month start, 0.0
    until 12 months of history exist) and returns that month's mu_t."""
    rng = np.random.default_rng(seed)
    n_months = int((years + warmup_years) * 12)
    price = {"A": 1.0, "B": 1.0}
    hist = {"A": [], "B": []}
    dates, prices_a, prices_b = [], [], []
    cur = _ts("2015-01-01")
    for _m in range(n_months):
        mu = {}
        for sym in ("A", "B"):
            trailing = (np.prod([1 + x for x in hist[sym][-12:]]) - 1) if len(hist[sym]) >= 12 else 0.0
            mu[sym] = mu_fn(sigma_m, trailing)
        z1 = rng.normal(0, 1, 30)
        z2 = rng.normal(0, 1, 30)
        d_a = mu["A"] / 30 + (sigma_m / np.sqrt(30)) * z1
        d_b = mu["B"] / 30 + (sigma_m / np.sqrt(30)) * (rho * z1 + np.sqrt(1 - rho ** 2) * z2)
        for i in range(30):
            price["A"] *= (1 + d_a[i])
            price["B"] *= (1 + d_b[i])
            dates.append(cur)
            prices_a.append(price["A"])
            prices_b.append(price["B"])
            cur += pd.Timedelta(days=1)
        hist["A"].append(np.prod(1 + d_a) - 1)
        hist["B"].append(np.prod(1 + d_b) - 1)

    idx = pd.DatetimeIndex(dates)
    spot = pd.DataFrame({"A": prices_a, "B": prices_b}, index=idx)

    month_grid = pd.date_range(idx[0].normalize(), idx[-1].normalize(), freq="MS", tz="UTC")
    warmup_end = month_grid[warmup_years * 12]
    scored = month_grid[(month_grid > warmup_end) & (month_grid <= month_grid[-1])]

    month_closes = pd.DataFrame({s: trend.month_end_closes(spot[s]) for s in ("A", "B")})
    r_tilde = trend.monthly_returns(month_closes)  # no funding in this synthetic world
    price_ret = r_tilde

    # rebalance_exposures(spot, T, ...) DECIDES the exposure at T = T_{h-1}
    # (using data known through T), to be HELD over the month ending at
    # T_h = T + 1 month. book_returns pairs e and r_tilde/price_ret by that
    # SAME index (the holding month's end), so the decision-time index must
    # be shifted forward one month before it is used as a book row -- else
    # e[T_h] would (mis)pair the exposure with the return of the month that
    # ENDS at its own decision instant, not the month it is actually held
    # over (a look-back mismatch, not a leak, but just as wrong).
    e_trend_rows, e_plus_rows = {}, {}
    for T in scored:
        e_h = T + pd.DateOffset(months=1)
        e_trend_rows[e_h], _ = trend.rebalance_exposures(spot, T, signal="trend")
        e_plus_rows[e_h], _ = trend.rebalance_exposures(spot, T, signal="plus_one")
    e_c = pd.DataFrame(e_trend_rows).T
    e_plus = pd.DataFrame(e_plus_rows).T

    br_c = trend.book_returns(e_c, r_tilde, price_ret, cost_side=0.0)
    br_plus = trend.book_returns(e_plus, r_tilde, price_ret, cost_side=0.0)
    n_c, _ = trend.signed_null(e_c, r_tilde, br_c["kappa"])
    x, d = trend.conditions(br_c["R"], n_c, br_plus["R"])
    return float(x.mean()), float(d.mean())


@pytest.mark.slow
def test_synthetic_trend_dgp_mostly_beats_null_and_control():
    def mu_trend(sigma_m, trailing):
        return 0.3 * sigma_m * np.sign(trailing) if trailing != 0 else 0.0

    x_pass = d_pass = 0
    for seed in range(10):
        mean_x, mean_d = _synthetic_run(seed, mu_trend)
        x_pass += mean_x > 0
        d_pass += mean_d > 0
    assert x_pass >= 9
    assert d_pass >= 8


@pytest.mark.slow
def test_synthetic_random_walk_with_drift_control_earns_the_drift_on_average():
    def mu_drift(sigma_m, trailing):
        return 0.5 * sigma_m

    d_means = []
    for seed in range(10):
        _mean_x, mean_d = _synthetic_run(seed, mu_drift)
        d_means.append(mean_d)
    assert np.mean(d_means) < 0
