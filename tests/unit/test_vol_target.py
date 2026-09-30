"""E15c (FROZEN 2026-09-30): tests for `model/vol_target.py`, written
before the module's logic was trusted -- each test is paired with a hand
mutation (recorded in the pre-reg's mutant table) to confirm it actually
fails without the intended behavior."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model.vol_target import (
    A0_MEAN_LOG_SIGMA,
    A0_SD,
    CLEAN_MONTHS,
    POWER_GATE,
    SIGMA_STAR,
    _regime_pair,
    choose_branch,
    moves_exposure,
    power_sim,
    score_pair,
    target_exposures,
)


def _daily_close(start: str, n: int, values) -> pd.Series:
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.Series(values, index=idx, dtype=float)


# --------------------------------------------------------------------------
# target_exposures
# --------------------------------------------------------------------------

def _flat_vol_close(start: str, n_months: int, sigma: float, seed: int = 0) -> pd.Series:
    """A daily close with the SAME sigma every month, so `month_sigma` is
    ~constant and a hand ratio is easy to reason about."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=1, freq="D", tz="UTC")[0] - pd.Timedelta(days=1)
    months = pd.period_range(pd.Period(start, freq="M"), periods=n_months, freq="M")
    rets = []
    for m in months:
        rets.append(rng.normal(0.0, sigma, m.days_in_month))
    rets = np.concatenate(rets)
    full_idx = pd.date_range(idx, periods=len(rets) + 1, freq="D", tz="UTC")
    close = pd.Series(np.exp(np.cumsum(np.concatenate([[0.0], rets]))) * 100.0, index=full_idx)
    return close


def test_target_exposures_hand_ratio_0_6():
    # sigma_hat_{t-1} (Jan, excl. last day) chosen so sigma_star/sigma_hat
    # == 0.6 exactly. Built via ARITHMETIC returns directly (cumprod), not
    # log-returns, so the constructed std survives pct_change exactly.
    sigma_hat = 0.05
    sigma_star = 0.6 * sigma_hat
    anchor = pd.date_range("2019-12-31", periods=1, freq="D", tz="UTC")
    jan_days = pd.date_range("2020-01-01", periods=31, freq="D", tz="UTC")
    feb_days = pd.date_range("2020-02-01", periods=29, freq="D", tz="UTC")
    all_idx = anchor.append(jan_days).append(feb_days)
    rng = np.random.default_rng(2)
    raw = rng.normal(0, 1, 31)
    jan_rets = raw / raw[:-1].std(ddof=1) * sigma_hat  # excl.-last-day std == sigma_hat exactly
    feb_rets = rng.normal(0, 0.01, 29)
    all_rets = np.concatenate([jan_rets, feb_rets])
    prices = np.concatenate([[100.0], 100.0 * np.cumprod(1.0 + all_rets)])
    close2 = pd.Series(prices, index=all_idx)
    r2 = close2 / close2.shift(1) - 1.0
    r2 = r2.dropna()
    e = target_exposures(r2, "2020-02", "2020-02", sigma_star, l_max=1.0)
    assert e.loc[pd.Period("2020-02", freq="M")] == pytest.approx(0.6, abs=1e-9)


def test_target_exposures_ratio_above_cap_clips_to_1_default_l_max():
    # Ratio 1.3 -- MUST use the default l_max, not an explicit 1.0, so a
    # mutant that changes the DEFAULT (l_max 1 -> 2) turns this red.
    jan_days = pd.date_range("2020-01-01", periods=31, freq="D", tz="UTC")
    feb_days = pd.date_range("2020-02-01", periods=29, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    jan_rets = rng.normal(0, 1, 31)
    sigma_hat = 0.02
    jan_rets = jan_rets / jan_rets[:-1].std(ddof=1) * sigma_hat
    feb_rets = rng.normal(0, 0.01, 29)
    all_idx = jan_days.append(feb_days)
    close = pd.Series(np.exp(np.cumsum(np.concatenate([jan_rets, feb_rets]))) * 100.0, index=all_idx)
    r = close / close.shift(1) - 1.0
    r = r.dropna()
    sigma_star = 1.3 * sigma_hat
    e = target_exposures(r, "2020-02", "2020-02", sigma_star)
    assert e.loc[pd.Period("2020-02", freq="M")] == pytest.approx(1.0, abs=1e-9)


def test_target_exposures_sigma_star_infinite_gives_one():
    close = _flat_vol_close("2020-01-01", 4, sigma=0.03, seed=4)
    r = close / close.shift(1) - 1.0
    r = r.dropna()
    e = target_exposures(r, "2020-02", "2020-04", np.inf)
    assert (e == 1.0).all()


def test_target_exposures_uses_sigma_hat_t_minus_1_not_t():
    """Two months with sharply different vol: exposure for month t must
    depend on month t-1's sigma, not t's own. (Mutant 1.)"""
    rng = np.random.default_rng(5)
    jan = rng.normal(0, 0.01, 31)   # low-vol month
    feb = rng.normal(0, 0.05, 29)   # high-vol month
    idx = pd.date_range("2020-01-01", periods=31, freq="D", tz="UTC").append(
        pd.date_range("2020-02-01", periods=29, freq="D", tz="UTC")
    )
    close = pd.Series(np.exp(np.cumsum(np.concatenate([jan, feb]))) * 100.0, index=idx)
    r = close / close.shift(1) - 1.0
    r = r.dropna()
    sigma_star = 0.02
    e_feb = target_exposures(r, "2020-02", "2020-02", sigma_star).iloc[0]
    # sigma_hat_jan ~ 0.01 -> ratio ~2 -> capped at 1.0. If the code used
    # Feb's OWN sigma (~0.05) instead, ratio ~0.4, NOT 1.0.
    assert e_feb == pytest.approx(1.0, abs=1e-9)


def test_target_exposures_truncation_property_20_random_months():
    """Recomputing e_t from data truncated to close <= T_{t-1} must equal
    the value computed from the full series (A4's property, generalized
    over random months)."""
    close = _flat_vol_close("2017-10-01", 30, sigma=0.03, seed=6)
    r_full = close / close.shift(1) - 1.0
    r_full = r_full.dropna()
    months = pd.period_range("2017-11", periods=28, freq="M")
    rng = np.random.default_rng(9)
    chosen = rng.choice(months, size=20, replace=False)
    sigma_star = 0.025
    for month in chosen:
        month = pd.Period(month, freq="M")
        e_full = target_exposures(r_full, month, month, sigma_star).iloc[0]
        cutoff = pd.Timestamp(month.start_time, tz="UTC")  # first day of month t = T_{t-1}
        truncated_close = close.loc[close.index < cutoff]
        r_trunc = truncated_close / truncated_close.shift(1) - 1.0
        r_trunc = r_trunc.dropna()
        e_trunc = target_exposures(r_trunc, month, month, sigma_star).iloc[0]
        assert e_full == e_trunc, f"{month}: {e_full} != {e_trunc}"


# --------------------------------------------------------------------------
# moves_exposure (A6)
# --------------------------------------------------------------------------

def test_moves_exposure_hand_series_ok():
    # 27 months: 9 (= 27/3) months at 0.5 (< 0.95) and 18 months at 0.96
    # (>= 0.95, not counted) -> ebar well under 0.90, n_below exactly 9.
    e = pd.Series([0.5] * 9 + [0.96] * 18)
    ebar, n_below, ok = moves_exposure(e)
    assert ebar <= 0.90
    assert n_below == 9
    assert ok is True


def test_moves_exposure_fails_when_ebar_too_high():
    e = pd.Series([0.95] * 27)  # every month at the A6 boundary (not below), ebar > 0.90
    ebar, n_below, ok = moves_exposure(e)
    assert ebar > 0.90
    assert ok is False


def test_moves_exposure_fails_when_too_few_months_below():
    # ebar low enough, but only 8 of 27 (< 1/3) months below 0.95.
    e = pd.Series([0.5] * 8 + [0.96] * 19)
    ebar, n_below, ok = moves_exposure(e)
    assert ebar <= 0.90
    assert n_below == 8
    assert ok is False


def test_moves_exposure_threshold_is_0_95_not_1_ki_mutant9():
    """Mutant 9: e < 1 instead of e < 0.95. A month at exactly 0.97 is
    NOT below 0.95 and must not count toward n_below."""
    e = pd.Series([0.97] * 9 + [0.80] * 18)  # ebar well below 0.90
    ebar, n_below, ok = moves_exposure(e)
    assert n_below == 18  # only the 0.80 months, not the 0.97 ones
    assert ok is True  # 18*3 >= 27


def test_moves_exposure_boundary_e_equal_0_95_not_counted():
    e = pd.Series([0.95] * 27)
    ebar, n_below, ok = moves_exposure(e)
    assert n_below == 0


# --------------------------------------------------------------------------
# score_pair -- identity + mutant-3/4/5 coverage, cheap (no 500 seeds)
# --------------------------------------------------------------------------

def _two_month_pair(seed_a=10, seed_b=11):
    close_a = _flat_vol_close("2020-01-01", 3, sigma=0.03, seed=seed_a)
    close_b = _flat_vol_close("2020-01-01", 3, sigma=0.03, seed=seed_b)
    return close_a, close_b, pd.Period("2020-02", freq="M"), pd.Period("2020-03", freq="M")


def test_score_pair_identity_sigma_star_infinite_gives_exact_zero_ci_and_fail():
    close_a, close_b, first, last = _two_month_pair()
    result = score_pair(close_a, close_b, first, last, sigma_star=np.inf,
                         cost_side=0.0011, boot_reps=200)
    for sym in result["symbols"].values():
        assert sym["ebar"] == pytest.approx(1.0)
        assert sym["ci_bh"] == (pytest.approx(0.0), pytest.approx(0.0), pytest.approx(0.0))
        assert sym["ci_ebar"] == (pytest.approx(0.0), pytest.approx(0.0), pytest.approx(0.0))
        assert sym["pass"] is False  # symbol_pass(0, 0) -- strict '>' required (mutant 5)
    assert result["overall"] == "FAIL"


def test_score_pair_ebar_is_full_window_mean_not_first_half_mutant3():
    """Construct exposures via a real (non-flat) close so the first half
    and full-window means of e differ, then check the reported ebar
    matches the FULL window."""
    rng = np.random.default_rng(12)
    # 4 scored months with rising vol so sigma_hat rises -> e falls over
    # time; first-half mean of e != full-window mean.
    months_sigma = [0.01, 0.01, 0.05, 0.05]
    idx0 = pd.date_range("2019-12-01", periods=31, freq="D", tz="UTC")  # warm-up month
    rets = [rng.normal(0, 0.01, 31)]
    month_periods = pd.period_range("2020-01", periods=4, freq="M")
    for sigma, m in zip(months_sigma, month_periods):
        rets.append(rng.normal(0, sigma, m.days_in_month))
    all_idx = idx0
    for m in month_periods:
        all_idx = all_idx.append(pd.date_range(m.start_time.tz_localize("UTC"), periods=m.days_in_month, freq="D"))
    all_rets = np.concatenate(rets)
    close = pd.Series(np.exp(np.cumsum(all_rets)) * 100.0, index=all_idx)
    e_only = target_exposures(close.pipe(lambda c: (c / c.shift(1) - 1.0).dropna()),
                               month_periods[0], month_periods[-1], sigma_star=0.02)
    first_half_mean = float(e_only.iloc[:2].mean())
    full_mean = float(e_only.mean())
    assert first_half_mean != pytest.approx(full_mean), "test fixture must actually differ half vs full"

    result = score_pair(close, close, month_periods[0], month_periods[-1],
                         sigma_star=0.02, cost_side=0.0, boot_reps=50)
    reported_ebar = result["symbols"]["a"]["ebar"]
    assert reported_ebar == pytest.approx(full_mean)
    assert reported_ebar != pytest.approx(first_half_mean)


def test_score_pair_bootstrap_is_paired_not_independent_mutant4():
    """Sanity that the SAME index draw is applied to both legs of each CI
    (paired_block_bootstrap, reused unmodified): with sigma_star=inf the
    two arrays fed to each bootstrap are bitwise identical, so a paired
    draw gives EXACTLY zero every rep -- an independent-index mutant would
    make the CI's width nonzero."""
    close_a, close_b, first, last = _two_month_pair()
    result = score_pair(close_a, close_b, first, last, sigma_star=np.inf, boot_reps=500)
    _, lo, hi = result["symbols"]["a"]["ci_bh"]
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.0)


# --------------------------------------------------------------------------
# _regime_pair generator -- pins the A0 DGP against E15b's test-tuned values
# --------------------------------------------------------------------------

def test_regime_pair_stationary_sd_pinned_at_0_4_not_0_6():
    """Mutant 10: A0 generator run at ln-sigma sd 0.6 (E15b's test value)
    instead of the pre-reg's pinned 0.4. Measured on a long path past
    burn-in, using power_sim/generator DEFAULTS only (no explicit sd=
    kwarg), so mutating the module's A0_SD default is what this test
    exercises."""
    _, _, _, _, ln_sig = _regime_pair(seed=0, phi=0.9, scored_months=2000)
    late = ln_sig[500:]
    assert float(np.std(late)) == pytest.approx(A0_SD, abs=0.05)
    assert float(np.mean(late)) == pytest.approx(A0_MEAN_LOG_SIGMA, abs=0.1)


def test_regime_pair_shares_sigma_path_correlated_shocks():
    close_a, close_b, first, last, _ = _regime_pair(seed=1, phi=0.9, scored_months=10)
    ra = (close_a / close_a.shift(1) - 1.0).dropna()
    rb = (close_b / close_b.shift(1) - 1.0).dropna()
    corr = np.corrcoef(ra.to_numpy(), rb.to_numpy())[0, 1]
    assert 0.6 < corr < 0.95  # correlated but not identical (corr=0.8, noisy sample)


def test_regime_pair_reproducible_under_seed():
    a1, b1, f1, l1, s1 = _regime_pair(seed=42, phi=0.9, scored_months=6)
    a2, b2, f2, l2, s2 = _regime_pair(seed=42, phi=0.9, scored_months=6)
    pd.testing.assert_series_equal(a1, a2)
    pd.testing.assert_series_equal(b1, b2)
    np.testing.assert_array_equal(s1, s2)


# --------------------------------------------------------------------------
# power_sim -- small/fast smoke test only (real 500-seed run is the A0
# report script, not the unit suite)
# --------------------------------------------------------------------------

def test_power_sim_reproducible_under_seeds_tiny():
    out1 = power_sim(months=4, phi=0.9, seeds=range(3), reps=30, max_workers=1)
    out2 = power_sim(months=4, phi=0.9, seeds=range(3), reps=30, max_workers=1)
    assert out1 == out2
    assert out1["n_paths"] == 3
    assert 0.0 <= out1["pass_rate"] <= 1.0


def test_power_sim_parallel_matches_serial():
    out_serial = power_sim(months=3, phi=0.9, seeds=range(2), reps=30, max_workers=1)
    out_parallel = power_sim(months=3, phi=0.9, seeds=range(2), reps=30, max_workers=2)
    assert out_serial == out_parallel


# --------------------------------------------------------------------------
# choose_branch -- full truth table (mutant 6: gate 0.4 instead of 0.5)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("clean_power,contam_power,expected", [
    (0.60, 0.10, "clean"),
    (0.50, 0.10, "clean"),           # boundary: >= gives clean
    (0.45, 0.60, "contaminated"),    # kills the "gate 0.4" mutant (0.45 >= 0.4 would wrongly pick clean)
    (0.30, 0.50, "contaminated"),
    (0.30, 0.49, "VOID_UNDERPOWERED"),
    (0.0, 0.0, "VOID_UNDERPOWERED"),
])
def test_choose_branch_truth_table(clean_power, contam_power, expected):
    a0 = {"clean": {"power": clean_power}, "contaminated": {"power": contam_power}}
    assert choose_branch(a0) == expected


def test_choose_branch_no_contaminated_key_is_underpowered_if_clean_fails():
    a0 = {"clean": {"power": 0.2}}
    assert choose_branch(a0) == "VOID_UNDERPOWERED"


def test_a0_clean_months_constant_is_27():
    assert CLEAN_MONTHS == 27


def test_sigma_star_matches_50_percent_annual():
    assert SIGMA_STAR == pytest.approx(0.50 / np.sqrt(365.0))
