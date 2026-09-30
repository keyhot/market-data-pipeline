"""Tests for `model/vol_managed.py` (E15b (i) vol-managed exposure).
Pre-registered in the vault, `Docs/vol-managed-exposure-experiment.md`
(+ Amendment 1, which supersedes the frozen primary-comparator text and
`symbol_pass`'s signature)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model.vol_managed import (
    COST_SIDE,
    L_MAX,
    MIN_SIGMA_DAYS,
    Q_HIGH,
    Q_LOW,
    constant_exposure_book,
    daily_returns,
    delta_es,
    exposure,
    exposures,
    expected_shortfall,
    lw_sharpe_diff,
    managed_daily,
    month_sigma,
    overall_verdict,
    paired_block_bootstrap,
    permuted_exposures,
    sigma_target,
    stage_a_valid,
    stage_c_verdict,
    symbol_pass,
    vol_state,
)


def _daily_close(start: str, n: int, values) -> pd.Series:
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.Series(values, index=idx, dtype=float)


# --------------------------------------------------------------------------
# daily_returns
# --------------------------------------------------------------------------

def test_daily_returns_basic():
    close = _daily_close("2020-01-01", 4, [100.0, 110.0, 99.0, 99.0])
    r = daily_returns(close)
    assert len(r) == 3
    assert r.iloc[0] == pytest.approx(0.10)
    assert r.iloc[1] == pytest.approx(99.0 / 110.0 - 1.0)
    assert r.iloc[2] == pytest.approx(0.0)
    # indexed by the day D (open date) -- no shift
    assert r.index[0] == close.index[1]


# --------------------------------------------------------------------------
# sigma_hat (month_sigma)
# --------------------------------------------------------------------------

def test_month_sigma_excludes_last_day_and_uses_ddof1():
    # A hand-built month: 16 days of returns (so 15 remain after excluding
    # the last), the last day is an outlier that must NOT move sigma_hat.
    n = 17  # -> 16 returns
    closes = [100.0]
    rng = np.random.default_rng(0)
    normal_rets = rng.normal(0, 0.01, n - 2)
    for r in normal_rets:
        closes.append(closes[-1] * (1 + r))
    closes.append(closes[-1] * 5.0)  # the excluded, outlier last day
    close = _daily_close("2021-03-01", n, closes)
    r = daily_returns(close)
    sig = month_sigma(r)
    expected = float(np.std(normal_rets, ddof=1))
    assert sig.loc[pd.Period("2021-03", freq="M")] == pytest.approx(expected)


def test_month_sigma_nan_below_min_days():
    # 14 usable returns (15 daily returns total, minus the excluded last
    # day = 14) -> NaN.
    n = 16  # 15 returns, 14 after excluding the last
    close = _daily_close("2021-04-01", n, [100.0 * 1.001 ** i for i in range(n)])
    r = daily_returns(close)
    sig = month_sigma(r)
    assert np.isnan(sig.loc[pd.Period("2021-04", freq="M")])


def test_2017_08_is_not_a_sigma_month():
    # 2017-08-18 .. 2017-08-31 = 14 days of returns (13 daily returns after
    # the first close), minus the excluded last day = 13 usable -> NaN.
    idx = pd.date_range("2017-08-17", "2017-08-31", freq="D", tz="UTC")
    close = pd.Series([100.0 * 1.001 ** i for i in range(len(idx))], index=idx)
    r = daily_returns(close)
    assert len(r) == 14  # 2017-08-18 .. 2017-08-31; 13 remain after excluding the last
    sig = month_sigma(r)
    assert np.isnan(sig.loc[pd.Period("2017-08", freq="M")])


# --------------------------------------------------------------------------
# sigma_target
# --------------------------------------------------------------------------

def test_sigma_target_includes_month_last_day_and_nothing_after():
    idx = pd.date_range("2020-01-01", "2020-03-31", freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    rets = rng.normal(0, 0.02, len(idx))
    r = pd.Series(rets, index=idx)
    target = sigma_target(r, "2020-02")
    expected = float(np.std(r[r.index <= pd.Timestamp("2020-02-29", tz="UTC")].to_numpy(), ddof=1))
    assert target == pytest.approx(expected)
    # nothing from March enters
    with_march = float(np.std(r.to_numpy(), ddof=1))
    assert target != pytest.approx(with_march)


def test_sigma_target_truncation_property_20_random_months():
    idx = pd.date_range("2018-01-01", "2022-12-31", freq="D", tz="UTC")
    rng = np.random.default_rng(2)
    r = pd.Series(rng.normal(0, 0.02, len(idx)), index=idx)
    months = pd.period_range("2018-06", "2022-11", freq="M")
    chosen = rng.choice(len(months), size=20, replace=False)
    for i in chosen:
        month = months[i]
        target = sigma_target(r, month)
        cutoff = month.to_timestamp(how="end").tz_localize("UTC")
        expected = float(np.std(r[r.index <= cutoff].to_numpy(), ddof=1))
        assert target == pytest.approx(expected)
        # truncation: nothing after month's last day
        after = r.index > cutoff
        if after.any():
            with_extra = float(np.std(r.to_numpy(), ddof=1))
            if len(r[after]) > 0:
                assert target != pytest.approx(with_extra) or not after.any()


# --------------------------------------------------------------------------
# vol_state
# --------------------------------------------------------------------------

def test_vol_state_top_bottom_middle():
    # vol_state's hist is CUMULATIVE (every value up to and including the
    # month being classified), so "top"/"bottom"/"middle" are each checked
    # at the LAST month of their own hand-built history, where the full
    # 10-value hist is in scope.
    months = pd.period_range("2020-01", periods=10, freq="M")
    hist_high = pd.Series([float(i) for i in range(1, 11)], index=months)  # 1..10
    assert vol_state(hist_high, months[-1]) == "high"  # last value 10, the max

    hist_low = pd.Series([float(i) for i in range(10, 0, -1)], index=months)  # 10..1
    assert vol_state(hist_low, months[-1]) == "low"  # last value 1, the min

    hist_medium = pd.Series([1.0, 10.0, 2.0, 9.0, 3.0, 8.0, 4.0, 7.0, 5.0, 6.0], index=months)
    assert vol_state(hist_medium, months[-1]) == "medium"  # last value 6, mid-pack


def test_vol_state_boundary_equal_to_q80_is_high():
    months = pd.period_range("2020-01", periods=10, freq="M")
    values = [float(i) for i in range(1, 11)]
    hist = pd.Series(values, index=months)
    q_hi = np.quantile(np.array(values), Q_HIGH, method="linear")
    # find the month whose value equals q_hi (may be interpolated; use one
    # constructed exactly on the boundary instead for a crisp check)
    boundary_hist = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=months[:5])
    q80 = np.quantile(boundary_hist.to_numpy(), Q_HIGH, method="linear")
    # month with value == q80 exactly
    exact_month = pd.Period("2020-06", freq="M")
    hist2 = pd.concat([boundary_hist, pd.Series([q80], index=[exact_month])])
    assert vol_state(hist2, exact_month) == "high"


def test_vol_state_hist_includes_month_itself():
    months = pd.period_range("2020-01", periods=3, freq="M")
    hist = pd.Series([1.0, 100.0, 1.0], index=months)
    # at months[1], hist = [1, 100] (2 values) since <= month inclusive;
    # 100 is the max => high
    assert vol_state(hist, months[1]) == "high"


# --------------------------------------------------------------------------
# exposure
# --------------------------------------------------------------------------

def test_exposure_high_below_cap():
    assert exposure("high", 0.6, 1.0, L_MAX) == pytest.approx(0.6)


def test_exposure_high_capped():
    assert exposure("high", 1.3, 1.0, L_MAX) == pytest.approx(1.0)


def test_exposure_low_verbatim_no_floor():
    assert exposure("low", 0.9, 1.0, L_MAX) == pytest.approx(0.9)


def test_exposure_medium_is_one():
    assert exposure("medium", 0.1, 5.0, L_MAX) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# exposures() -- month range built from first/last, not from r's months
# (A4's truncated call must not KeyError/skip)
# --------------------------------------------------------------------------

def _synthetic_daily_returns(seed: int, start: str, n_days: int, vol: float = 0.02) -> pd.Series:
    idx = pd.date_range(start, periods=n_days, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, vol, n_days)
    log_price = np.cumsum(np.concatenate([[0.0], rets]))
    close = pd.Series(np.exp(log_price[1:]), index=idx)
    return daily_returns(close)


def test_exposures_month_range_from_first_last_not_from_r():
    # r covers 2018-01 .. 2021-12, but `first`/`last` ask for a range whose
    # early months would have NO entry in r if we (wrongly) built the
    # month loop from r's own observed months post-truncation.
    r = _synthetic_daily_returns(3, "2018-01-01", 1461)
    e = exposures(r, "2020-01", "2020-06", oracle=False)
    assert list(e.index) == list(pd.period_range("2020-01", "2020-06", freq="M"))
    assert e.notna().all()


def test_exposures_oracle_uses_own_month_and_inclusive_hist():
    r = _synthetic_daily_returns(4, "2018-01-01", 1461)
    e_oracle = exposures(r, "2020-06", "2020-06", oracle=True)
    assert len(e_oracle) == 1
    assert 0.0 <= e_oracle.iloc[0] <= 1.0


# --------------------------------------------------------------------------
# managed_daily / holding
# --------------------------------------------------------------------------

def test_managed_daily_hand_computed_3day_month_half_exposure():
    # anchor day (last day of prior month) + 3 days of month t
    close = _daily_close("2020-02-29", 4, [100.0, 105.0, 95.0, 110.0])
    e = pd.Series([0.5], index=[pd.Period("2020-03", freq="M")])
    out = managed_daily(close, e, cost_side=0.0, e_start=0.5)  # no cost -> hand-checkable
    p0 = 100.0
    V1 = 0.5 + 0.5 * 105.0 / p0
    V2 = 0.5 + 0.5 * 95.0 / p0
    V3 = 0.5 + 0.5 * 110.0 / p0
    assert out["y"].iloc[0] == pytest.approx(V1 - 1.0)
    assert out["y"].iloc[1] == pytest.approx(V2 / V1 - 1.0)
    assert out["y"].iloc[2] == pytest.approx(V3 / V2 - 1.0)


def test_managed_daily_cost_charged_only_first_day():
    close = _daily_close("2020-02-29", 4, [100.0, 101.0, 102.0, 103.0])
    e = pd.Series([0.7], index=[pd.Period("2020-03", freq="M")])
    out = managed_daily(close, e, cost_side=COST_SIDE, e_start=1.0)
    assert out["kappa"].iloc[0] == pytest.approx(COST_SIDE * abs(0.7 - 1.0))
    assert (out["kappa"].iloc[1:] == 0.0).all()


def test_buy_and_hold_pays_zero_cost_after_start():
    close = _daily_close("2020-02-29", 4, [100.0, 101.0, 99.0, 102.0])
    e = pd.Series([1.0], index=pd.period_range("2020-03", periods=1, freq="M"))
    out = managed_daily(close, e, cost_side=COST_SIDE, e_start=1.0)
    assert (out["kappa"] == 0.0).all()
    r = daily_returns(close)
    for d in out.index:
        assert out.loc[d, "y"] == pytest.approx(r.loc[d])


def test_compounding_identity_a3_on_synthetic_data():
    r = _synthetic_daily_returns(5, "2020-01-01", 400)
    close = (1.0 + r).cumprod() * 100.0
    close = pd.concat([pd.Series([100.0], index=[close.index[0] - pd.Timedelta(days=1)]), close])
    e = exposures(r, "2020-02", "2020-12", oracle=False)
    out = managed_daily(close, e, cost_side=COST_SIDE, e_start=1.0)
    idx = pd.PeriodIndex(pd.DatetimeIndex(out.index), freq="M")
    for t in e.index:
        month_rows = out.loc[idx == t]
        prod = float((1.0 + month_rows["y"]).prod())
        days = close.index[pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M") == t]
        prior_days = close.index[pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M") == (t - 1)]
        p0 = float(close.loc[prior_days.max()])
        R_t = float(close.loc[days.max()]) / p0 - 1.0
        e_t = float(e.loc[t])
        kappa_t = float(month_rows["kappa"].iloc[0])
        expected = (1 - kappa_t) * (1 + e_t * R_t)
        assert prod == pytest.approx(expected, abs=1e-9)


# --------------------------------------------------------------------------
# constant_exposure_book / Amendment 1's "control that MUST differ"
# --------------------------------------------------------------------------

def test_constant_exposure_book_reuses_managed_daily_path():
    close = _daily_close("2019-12-31", 32, [100.0 + i for i in range(32)])
    out = constant_exposure_book(close, 0.6, "2020-01", "2020-01", cost_side=COST_SIDE, e_start=1.0)
    e = pd.Series([0.6], index=[pd.Period("2020-01", freq="M")])
    expected = managed_daily(close, e, cost_side=COST_SIDE, e_start=1.0)
    pd.testing.assert_frame_equal(out, expected)


def test_a2_identity_vm_equals_bh_bitwise_when_e_is_one():
    close = _daily_close("2019-12-31", 366, [100.0 * 1.0005 ** i for i in range(366)])
    months = pd.period_range("2020-01", "2020-12", freq="M")
    e_one = pd.Series(1.0, index=months)
    vm = managed_daily(close, e_one, cost_side=COST_SIDE, e_start=1.0)
    bh = constant_exposure_book(close, 1.0, "2020-01", "2020-12", cost_side=COST_SIDE, e_start=1.0)
    pd.testing.assert_frame_equal(vm, bh)
    assert delta_es(bh["y"].to_numpy(), vm["y"].to_numpy(), q=0.975) == pytest.approx(0.0, abs=0.0)


def test_amendment_control_that_must_differ_constant_book_equals_rule():
    # If the rule's own exposure equals the constant book (e ≡ e_bar every
    # month), (b)'s CI must straddle 0 -> FAIL. This is the mutation-check
    # target: dropping (b), or computing it at 1x, must fail this test.
    close = _daily_close("2019-12-31", 366, [100.0 * (1.0 + 0.001 * np.sin(i)) for i in range(366)])
    months = pd.period_range("2020-01", "2020-12", freq="M")
    e_bar = 0.6
    e_const = pd.Series(e_bar, index=months)
    rule_book = managed_daily(close, e_const, cost_side=COST_SIDE, e_start=1.0)
    ctrl_book = constant_exposure_book(close, e_bar, "2020-01", "2020-12", cost_side=COST_SIDE, e_start=1.0)
    pd.testing.assert_frame_equal(rule_book, ctrl_book)
    point, lo, hi = paired_block_bootstrap(
        ctrl_book["y"].to_numpy(), rule_book["y"].to_numpy(), delta_es, reps=200,
    )
    assert lo <= 0.0 <= hi
    assert not symbol_pass(ci_lo_bh=1.0, ci_lo_ebar=lo)


# --------------------------------------------------------------------------
# ES
# --------------------------------------------------------------------------

def test_expected_shortfall_hand_example():
    y = -np.arange(1, 101) / 100.0
    es = expected_shortfall(y, 0.975)
    assert es == pytest.approx(0.99)


def test_expected_shortfall_constant_series():
    y = np.full(50, -0.02)
    assert expected_shortfall(y, 0.99) == pytest.approx(0.02)


def test_expected_shortfall_scale_equivariant():
    rng = np.random.default_rng(6)
    y = rng.normal(0, 0.02, 200)
    es1 = expected_shortfall(y, 0.975)
    es2 = expected_shortfall(y * 2.0, 0.975)
    assert es2 == pytest.approx(es1 * 2.0)


# --------------------------------------------------------------------------
# By-construction property (documents ⚠️): ΔES >= 0 for any e in [0,1],
# zero cost.
# --------------------------------------------------------------------------

def test_by_construction_delta_es_nonnegative_zero_cost():
    rng = np.random.default_rng(11)
    close_base = _daily_close("2019-12-31", 366, [100.0] * 366)
    months = pd.period_range("2020-01", "2020-12", freq="M")
    for path in range(50):
        rets = rng.normal(0, 0.02, 365)
        log_price = np.cumsum(np.concatenate([[0.0], rets]))
        close = pd.Series(np.exp(log_price) * 100.0, index=close_base.index)
        e_vals = rng.uniform(0, 1, len(months))
        e = pd.Series(e_vals, index=months)
        bh = constant_exposure_book(close, 1.0, "2020-01", "2020-12", cost_side=0.0, e_start=1.0)
        vm = managed_daily(close, e, cost_side=0.0, e_start=1.0)
        d = delta_es(bh["y"].to_numpy(), vm["y"].to_numpy(), q=0.975)
        assert d >= -1e-12


# --------------------------------------------------------------------------
# The comparator that CAN fail (iid vs persistent regimes)
# --------------------------------------------------------------------------

def _regime_close(seed: int, persistent: bool, scored_months: int = 96,
                   warmup_months: int = 6, mean_log_sigma=np.log(0.03), phi=0.9, sd=0.6):
    """A synthetic daily close whose per-month vol follows an iid or
    persistent AR(1) log-sigma regime, with `warmup_months` of history
    BEFORE the scored window so `exposures()` has a real t-1 (and a
    non-trivial hist) for the first scored month -- mirrors the real
    run's 2017-08..2019-12 warm-up (not pinned in length by the note; see
    the stage-A "spec gaps" writeup)."""
    rng = np.random.default_rng(seed)
    months_n = warmup_months + scored_months
    if persistent:
        innov_sd = sd * np.sqrt(1 - phi ** 2)
        ln_sig = np.empty(months_n)
        ln_sig[0] = mean_log_sigma
        for i in range(1, months_n):
            ln_sig[i] = mean_log_sigma + phi * (ln_sig[i - 1] - mean_log_sigma) + rng.normal(0, innov_sd)
    else:
        ln_sig = rng.normal(mean_log_sigma, sd, months_n)
    sigmas = np.exp(ln_sig)

    start = pd.Timestamp("2018-01-01", tz="UTC") - pd.DateOffset(months=warmup_months) - pd.Timedelta(days=1)
    month_starts = pd.period_range(start + pd.Timedelta(days=1), periods=months_n, freq="M")
    all_rets = []
    for m, sigma in zip(month_starts, sigmas):
        n_days = m.days_in_month
        all_rets.append(rng.normal(0, sigma, n_days))
    rets_concat = np.concatenate(all_rets)
    close = pd.Series(np.exp(np.cumsum(np.concatenate([[0.0], rets_concat]))) * 100.0,
                       index=pd.date_range(start, periods=len(rets_concat) + 1, freq="D", tz="UTC"))
    first_scored = month_starts[warmup_months]
    last_scored = month_starts[-1]
    return close, first_scored, last_scored


def test_ebar_control_fails_in_iid_regimes_most_seeds():
    fails = 0
    for seed in range(10):
        close, first, last = _regime_close(seed, persistent=False)
        r = daily_returns(close)
        e = exposures(r, first, last, oracle=False)
        ebar = float(e.mean())
        vm = managed_daily(close, e, cost_side=0.0, e_start=1.0)
        ctrl = constant_exposure_book(close, ebar, first, last, cost_side=0.0, e_start=1.0)
        _, lo, _ = paired_block_bootstrap(ctrl["y"].to_numpy(), vm["y"].to_numpy(), delta_es, reps=200)
        if lo <= 0:
            fails += 1
    assert fails >= 8


def test_ebar_control_passes_in_persistent_regimes_most_seeds():
    passes = 0
    for seed in range(10):
        close, first, last = _regime_close(seed, persistent=True)
        r = daily_returns(close)
        e = exposures(r, first, last, oracle=False)
        ebar = float(e.mean())
        vm = managed_daily(close, e, cost_side=0.0, e_start=1.0)
        ctrl = constant_exposure_book(close, ebar, first, last, cost_side=0.0, e_start=1.0)
        _, lo, _ = paired_block_bootstrap(ctrl["y"].to_numpy(), vm["y"].to_numpy(), delta_es, reps=200)
        if lo > 0:
            passes += 1
    assert passes >= 8


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

def test_bootstrap_pairing_identity_gives_zero_every_rep():
    rng = np.random.default_rng(0)
    a = rng.normal(0, 0.02, 300)
    point, lo, hi = paired_block_bootstrap(a, a, delta_es, reps=300)
    assert point == pytest.approx(0.0)
    assert lo == pytest.approx(0.0, abs=1e-12)
    assert hi == pytest.approx(0.0, abs=1e-12)


def test_bootstrap_reproducible_under_seed():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 0.02, 200)
    b = rng.normal(0, 0.02, 200)
    r1 = paired_block_bootstrap(a, b, delta_es, reps=200, seed=7)
    r2 = paired_block_bootstrap(a, b, delta_es, reps=200, seed=7)
    assert r1 == r2


# --------------------------------------------------------------------------
# LW
# --------------------------------------------------------------------------

def test_lw_identical_series_delta_zero():
    rng = np.random.default_rng(2)
    r1 = rng.normal(0.001, 0.02, 200)
    delta, lo, hi = lw_sharpe_diff(r1, r1, lag=5)
    assert delta == pytest.approx(0.0)
    assert lo <= 0.0 <= hi


def test_lw_lag_zero_matches_hand_computed_delta_method_5point():
    r1 = np.array([0.01, -0.02, 0.015, 0.0, -0.01])
    r2 = np.array([0.005, -0.01, 0.02, 0.01, -0.005])
    n = len(r1)
    mu1, mu2 = r1.mean(), r2.mean()
    g1, g2 = (r1 ** 2).mean(), (r2 ** 2).mean()
    s1, s2 = g1 - mu1 ** 2, g2 - mu2 ** 2
    expected_delta = mu1 / np.sqrt(s1) - mu2 / np.sqrt(s2)
    grad = np.array([
        g1 / s1 ** 1.5, -g2 / s2 ** 1.5,
        -mu1 / (2 * s1 ** 1.5), mu2 / (2 * s2 ** 1.5),
    ])
    d = np.column_stack([r1 - mu1, r2 - mu2, r1 ** 2 - g1, r2 ** 2 - g2])
    Psi = (d.T @ d) / n  # lag 0: no Bartlett terms
    var = float(grad @ Psi @ grad) / n
    se = np.sqrt(var)
    expected_lo = expected_delta - 1.959964 * se
    expected_hi = expected_delta + 1.959964 * se

    delta, lo, hi = lw_sharpe_diff(r1, r2, lag=0)
    assert delta == pytest.approx(expected_delta)
    assert lo == pytest.approx(expected_lo)
    assert hi == pytest.approx(expected_hi)


# --------------------------------------------------------------------------
# Verdict table (Amendment 1: symbol_pass is a conjunction of two CIs)
# --------------------------------------------------------------------------

def test_symbol_pass_conjunction():
    assert symbol_pass(ci_lo_bh=1.0, ci_lo_ebar=0.0) is False
    assert symbol_pass(ci_lo_bh=0.0, ci_lo_ebar=1.0) is False
    assert symbol_pass(ci_lo_bh=1.0, ci_lo_ebar=1.0) is True
    assert symbol_pass(ci_lo_bh=0.0, ci_lo_ebar=0.0) is False
    assert symbol_pass(ci_lo_bh=1e-12, ci_lo_ebar=1e-12) is True


def test_overall_verdict_over_pairs():
    assert overall_verdict({"BTCUSDT": True, "ETHUSDT": True}) == "PASS"
    assert overall_verdict({"BTCUSDT": True, "ETHUSDT": False}) == "SPLIT"
    assert overall_verdict({"BTCUSDT": False, "ETHUSDT": True}) == "SPLIT"
    assert overall_verdict({"BTCUSDT": False, "ETHUSDT": False}) == "FAIL"


def test_verdict_reads_vs_bh_and_vs_ebar_distinctly_ki094():
    # KI-094 lesson, updated for Amendment 1: BOTH CIs must be read; feed
    # distinct values and assert the function actually uses both.
    assert symbol_pass(ci_lo_bh=5.0, ci_lo_ebar=-5.0) is False
    assert symbol_pass(ci_lo_bh=-5.0, ci_lo_ebar=5.0) is False


def test_stage_c_verdict():
    assert stage_c_verdict(0.001) == "CONFIRMED"
    assert stage_c_verdict(-0.001) == "NOT_CONFIRMED"
    assert stage_c_verdict(0.0) == "NOT_CONFIRMED"


def test_stage_a_valid_returns_failing_ids():
    valid, failing = stage_a_valid({"a1": True, "a2": False, "a3": True, "a4": False})
    assert valid is False
    assert failing == ["a2", "a4"]
    valid2, failing2 = stage_a_valid({"a1": True, "a2": True})
    assert valid2 is True
    assert failing2 == []


# --------------------------------------------------------------------------
# permuted_exposures
# --------------------------------------------------------------------------

def test_permuted_exposures_same_multiset_reproducible():
    months = pd.period_range("2020-01", periods=12, freq="M")
    e = pd.Series(np.linspace(0.2, 1.0, 12), index=months)
    p1 = permuted_exposures(e, seed=7)
    p2 = permuted_exposures(e, seed=7)
    pd.testing.assert_series_equal(p1, p2)
    assert sorted(p1.to_numpy()) == pytest.approx(sorted(e.to_numpy()))
    assert list(p1.index) == list(e.index)
