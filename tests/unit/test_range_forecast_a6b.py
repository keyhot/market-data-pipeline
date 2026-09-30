"""E15 A6b: pure-function tests for model/range_forecast_a6b.py -- the
post-freeze, report-only synthetic strictness diagnostic. Never gates E15's
verdict (see the vault's 'A6b' section, Docs/range-forecast-experiment.md).
Reads no real data; every fixture here is synthetic or hand-built."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model.range_forecast import ALPHA_PRIMARY, lr_uc
from model.range_forecast_a6b import (
    calibrate_distortion_factor,
    distort_band,
    empirical_coverage,
    garch_t_dgp,
    uc_tolerant_pass,
    uc_tolerant_within,
)

UTC = "UTC"


# --------------------------------------------------------------------------
# distort_band
# --------------------------------------------------------------------------

def test_distort_band_k1_is_identity():
    rng = np.random.default_rng(0)
    L = -rng.uniform(0.5, 2.0, size=200)
    U = rng.uniform(0.5, 2.0, size=200)
    L2, U2 = distort_band(L, U, 1.0)
    np.testing.assert_allclose(L2, L, atol=1e-12)
    np.testing.assert_allclose(U2, U, atol=1e-12)


def test_distort_band_preserves_midpoint():
    L = np.array([-1.0, -3.0, -0.2])
    U = np.array([2.0, 1.0, 0.6])
    mid_before = (L + U) / 2.0
    L2, U2 = distort_band(L, U, 0.5)
    mid_after = (L2 + U2) / 2.0
    np.testing.assert_allclose(mid_after, mid_before, atol=1e-12)


def test_distort_band_k_less_than_1_narrows():
    L = np.array([-1.0])
    U = np.array([1.0])
    L2, U2 = distort_band(L, U, 0.5)
    assert (U2 - L2) < (U - L)
    assert (U2 - L2) == pytest.approx(1.0)  # half the original width


def test_distort_band_k_greater_than_1_widens():
    L = np.array([-1.0])
    U = np.array([1.0])
    L2, U2 = distort_band(L, U, 2.0)
    assert (U2 - L2) == pytest.approx(4.0)


# --------------------------------------------------------------------------
# empirical_coverage
# --------------------------------------------------------------------------

def test_empirical_coverage_matches_manual_count():
    r = np.array([0.0, 0.0, 5.0, -5.0, 0.0])
    L = np.full(5, -1.0)
    U = np.full(5, 1.0)
    # 2 of 5 rows miss -> coverage 0.6
    assert empirical_coverage(r, L, U) == pytest.approx(0.6)


# --------------------------------------------------------------------------
# calibrate_distortion_factor
# --------------------------------------------------------------------------

def test_calibrate_distortion_factor_hits_target_coverage():
    """A large synthetic sample with a *correctly* calibrated 80% band
    (normal quantiles) should calibrate a k that hits an 79.5% and a 78.0%
    target on the SAME sample, to within the bisection's own tolerance."""
    rng = np.random.default_rng(42)
    n = 200_000
    sigma = rng.uniform(0.5, 2.0, size=n)
    z_lo, z_hi = -1.2815515655446004, 1.2815515655446004  # exact 80% normal quantiles
    r = rng.normal(0.0, 1.0, size=n) * sigma
    L = z_lo * sigma
    U = z_hi * sigma
    baseline = empirical_coverage(r, L, U)
    assert baseline == pytest.approx(0.80, abs=0.01)

    for target in (0.795, 0.780):
        k, achieved = calibrate_distortion_factor(r, L, U, target, tol=1e-5)
        assert achieved == pytest.approx(target, abs=2e-4)
        L2, U2 = distort_band(L, U, k)
        assert empirical_coverage(r, L2, U2) == pytest.approx(target, abs=2e-4)


def test_calibrate_distortion_factor_k1_reproduces_original_coverage():
    rng = np.random.default_rng(7)
    n = 50_000
    r = rng.normal(0.0, 1.0, size=n)
    L = np.full(n, -1.2815515655446004)
    U = np.full(n, 1.2815515655446004)
    target = empirical_coverage(r, L, U)
    k, achieved = calibrate_distortion_factor(r, L, U, target, tol=1e-5)
    assert k == pytest.approx(1.0, abs=1e-2)


def test_calibrate_distortion_factor_raises_outside_bracket():
    r = np.zeros(100)
    L = np.full(100, -1.0)
    U = np.full(100, 1.0)
    with pytest.raises(ValueError):
        calibrate_distortion_factor(r, L, U, target_coverage=2.0, k_lo=0.5, k_hi=1.5)


# --------------------------------------------------------------------------
# uc_tolerant_pass / uc_tolerant_within -- boundary is a mutation target
# --------------------------------------------------------------------------

def test_uc_tolerant_within_exact_boundary_passes():
    """Mutant target: the ±2pp tolerance boundary must be INCLUSIVE (<=),
    computed in integer hit-counts (not float coverage) so the comparison
    never depends on float rounding at the boundary itself. n=1000,
    alpha=0.2 -> center=200, tol=0.02 -> band=20. n1=220 is exactly on the
    boundary and must pass; n1=221 is one hit past it and must fail."""
    assert uc_tolerant_within(n1=220, n=1000, alpha=0.2, tol=0.02) is True
    assert uc_tolerant_within(n1=180, n=1000, alpha=0.2, tol=0.02) is True  # symmetric
    assert uc_tolerant_within(n1=221, n=1000, alpha=0.2, tol=0.02) is False
    assert uc_tolerant_within(n1=179, n=1000, alpha=0.2, tol=0.02) is False


def test_uc_tolerant_within_center_always_passes():
    assert uc_tolerant_within(n1=200, n=1000, alpha=0.2, tol=0.02) is True


def _hits_with_exact_miss_count(n1: int, n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    hits = np.zeros(n, dtype=int)
    idx = rng.choice(n, size=n1, replace=False)
    hits[idx] = 1
    return hits


def test_uc_tolerant_pass_rejects_far_miscalibration_even_with_tolerance():
    """A miss rate far outside the ±2pp band (e.g. 40% instead of 20%)
    must fail both the strict LR test and the tolerant rule."""
    hits = _hits_with_exact_miss_count(n1=400, n=1000)
    passed, p_uc = uc_tolerant_pass(hits, ALPHA_PRIMARY)
    assert passed is False
    _, p_uc_direct = lr_uc(hits, ALPHA_PRIMARY)
    assert p_uc == pytest.approx(p_uc_direct)


def test_uc_tolerant_pass_accepts_within_tolerance_even_if_strict_rejects():
    """At large n the strict LR test rejects deviations tighter than
    ±2pp (the vault's open question: ~±0.4pp at stage-B n), so a
    within-tolerance-but-strict-fail case must exist and must pass under
    the tolerant rule."""
    n = 43_800
    center = round(ALPHA_PRIMARY * n)
    band = round(0.02 * n)
    n1 = center + band  # exactly on the tolerance boundary
    hits = _hits_with_exact_miss_count(n1=n1, n=n, seed=1)
    _, p_uc = lr_uc(hits, ALPHA_PRIMARY)
    assert p_uc < 0.05  # strict test rejects at this n (confirms the open question)
    passed, _ = uc_tolerant_pass(hits, ALPHA_PRIMARY)
    assert passed is True


def test_uc_tolerant_pass_strict_pass_implies_tolerant_pass():
    """The tolerant rule is an OR with the strict rule, so it can never be
    stricter than the frozen rule."""
    rng = np.random.default_rng(3)
    n = 2000
    hits = (rng.random(n) < ALPHA_PRIMARY).astype(int)
    _, p_uc = lr_uc(hits, ALPHA_PRIMARY)
    if p_uc >= 0.05:
        passed, _ = uc_tolerant_pass(hits, ALPHA_PRIMARY)
        assert passed is True


# --------------------------------------------------------------------------
# garch_t_dgp -- family misspecification DGP (outside log-HAR)
# --------------------------------------------------------------------------

def test_garch_t_dgp_output_shape_matches_seasonal_dgp():
    from model.range_forecast import seasonal_dgp
    a = garch_t_dgp(seed=0, years=0.2)
    b = seasonal_dgp(seed=0, years=0.2)
    assert list(a.columns) == list(b.columns)
    assert len(a) == len(b)
    assert (a["timestamp"].iloc[0] == b["timestamp"].iloc[0])
    assert (a["timestamp"].diff().dropna() == pd.Timedelta(minutes=15)).all()


def test_garch_t_dgp_no_nans_and_positive_prices():
    bars = garch_t_dgp(seed=1, years=1.0)
    assert bars["close"].notna().all()
    assert (bars["close"] > 0).all()
    assert np.isfinite(bars["close"]).all()


def test_garch_t_dgp_deterministic_per_seed():
    a = garch_t_dgp(seed=5, years=0.1)
    b = garch_t_dgp(seed=5, years=0.1)
    pd.testing.assert_frame_equal(a, b)


def test_garch_t_dgp_seeds_differ():
    a = garch_t_dgp(seed=5, years=0.1)
    b = garch_t_dgp(seed=6, years=0.1)
    assert not a["close"].equals(b["close"])


def test_garch_t_dgp_unconditional_variance_roughly_matches_target():
    """Loose sanity bound only (persistence 0.98 gives high variance-of-
    variance): the sample variance of hourly log returns over a long run
    should be within an order of magnitude of the target."""
    target = 2.5e-5
    bars = garch_t_dgp(seed=9, years=8.0, unconditional_var=target)
    close = bars.set_index("timestamp")["close"]
    hourly = close[close.index.minute == 0]
    r = np.log(hourly).diff().dropna()
    sample_var = float(np.var(r))
    assert target / 5 < sample_var < target * 5


def test_garch_t_dgp_seasonal_false_matches_seasonal_true_shape():
    a = garch_t_dgp(seed=2, years=0.1, seasonal=False)
    b = garch_t_dgp(seed=2, years=0.1, seasonal=True)
    assert len(a) == len(b)
    assert not a["close"].equals(b["close"])  # different vol path -> different prices


def test_garch_t_dgp_requires_stationary_persistence():
    with pytest.raises(ValueError):
        garch_t_dgp(seed=0, years=0.1, a=0.6, b=0.6)  # a+b > 1
