"""E15 A6b: post-freeze, report-only synthetic strictness diagnostic.

**Never gates E15's verdict.** A6 (frozen, `Docs/range-forecast-experiment.md`)
showed a CORRECT model (log-HAR DGP == the candidate's own family) passes
the frozen UC/CC/DQ conjunction 10/10 at stage-B scale -- useful, but it
only proves a correct model passes. A6b answers the sharper question the
vault's open "UC strictness at this sample size" question needs: how does
the frozen conjunction behave on a model that is slightly WRONG -- either a
controlled true-coverage deficit (the interval is a bit too narrow) or a
DGP outside log-HAR entirely?

Everything here is synthetic. It reads NO real market data, is imported
only by `scripts/e15_a6b_report.py` and this module's own tests, and its
result is a report, never a gate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from model.range_forecast import RV_FLOOR, hour_of_week, lr_uc, miss

_T5_SCALE = float(np.sqrt(3.0 / 5.0))  # rescales Student-t(5) (var = 5/3) to unit variance


# --------------------------------------------------------------------------
# Controlled coverage deficit
# --------------------------------------------------------------------------

def distort_band(L, U, k: float):
    """Rescale an interval [L, U] around its own midpoint by factor `k`.
    `k=1` is an exact identity; `k<1` narrows (lowers true coverage);
    `k>1` widens (raises it). Pure, elementwise."""
    L = np.asarray(L, dtype=float)
    U = np.asarray(U, dtype=float)
    mid = (L + U) / 2.0
    half = (U - L) / 2.0
    return mid - k * half, mid + k * half


def empirical_coverage(r, L, U) -> float:
    """1 - miss rate, using the frozen `miss()` definition."""
    return float(1.0 - miss(r, L, U).mean())


def calibrate_distortion_factor(r, L, U, target_coverage: float,
                                 k_lo: float = 0.1, k_hi: float = 3.0,
                                 tol: float = 1e-5, max_iter: int = 60) -> tuple[float, float]:
    """Bisect `k` so `empirical_coverage(r, *distort_band(L, U, k))` hits
    `target_coverage` on THIS sample (a large, separate-seed simulation is
    the caller's job, not this function's). Coverage is monotonically
    non-decreasing in `k` (a wider interval never covers less). Returns
    `(k, achieved_coverage)` -- the achieved value is what actually gets
    reported, never assumed equal to the target."""
    def cov(k: float) -> float:
        Lk, Uk = distort_band(L, U, k)
        return empirical_coverage(r, Lk, Uk)

    cov_lo, cov_hi = cov(k_lo), cov(k_hi)
    if not (cov_lo <= target_coverage <= cov_hi):
        raise ValueError(
            f"target coverage {target_coverage} outside the bracket "
            f"[{cov_lo}, {cov_hi}] reachable by k in [{k_lo}, {k_hi}]"
        )
    for _ in range(max_iter):
        k_mid = (k_lo + k_hi) / 2.0
        c = cov(k_mid)
        if abs(c - target_coverage) <= tol:
            return k_mid, c
        if c < target_coverage:
            k_lo = k_mid
        else:
            k_hi = k_mid
    k_mid = (k_lo + k_hi) / 2.0
    return k_mid, cov(k_mid)


# --------------------------------------------------------------------------
# ±2pp UC tolerance
# --------------------------------------------------------------------------
#
# The frozen verdict rule's UC leg is the strict Christoffersen LR test
# (p_uc >= 0.05). The vault's open question ("UC strictness at this sample
# size") names a "±2pp tolerance" as the alternative, tracing to the
# pre-registration's ORIGINAL wording (Docs/model-improvement-plan.md, item
# B: "coverage within ±2pp of nominal") -- a literal coverage-band check,
# not a modified likelihood-ratio statistic. That is the reading
# implemented below: pass if EITHER the strict LR test passes, OR the
# empirical miss count is within `tol` of the nominal alpha*n. The OR
# means this can never be stricter than the frozen rule.

def uc_tolerant_within(n1: int, n: int, alpha: float, tol: float = 0.02) -> bool:
    """Is the empirical miss count `n1` (out of `n`) within `tol` of the
    nominal `alpha*n`? Computed in INTEGER counts (round-then-compare), not
    float coverage, so the boundary is exact rather than float-epsilon-
    dependent (`0.22 - 0.20 == 0.020000000000000018` in floats) -- the
    inequality is inclusive (`<=`) at both ends."""
    center = round(alpha * n)
    band = round(tol * n)
    return abs(n1 - center) <= band


def uc_tolerant_pass(hits, alpha: float, level: float = 0.05,
                      tol: float = 0.02) -> tuple[bool, float]:
    """`(passed, p_uc)`: passed iff the frozen strict UC test passes OR
    the empirical coverage is within `tol` of nominal (`uc_tolerant_within`).
    `p_uc` is the frozen LR test's p-value, returned for reporting either
    way."""
    hits = np.asarray(hits)
    n = len(hits)
    n1 = int(hits.sum())
    _, p_uc = lr_uc(hits, alpha)
    strict = bool(p_uc >= level)
    within_tol = uc_tolerant_within(n1, n, alpha, tol)
    return bool(strict or within_tol), float(p_uc)


# --------------------------------------------------------------------------
# Family misspecification: GARCH(1,1)-t(5), outside log-HAR
# --------------------------------------------------------------------------

def garch_t_dgp(seed: int, years: float, seasonal: bool = True,
                 start: pd.Timestamp = pd.Timestamp("2020-01-01", tz="UTC"),
                 a: float = 0.06, b: float = 0.92,
                 unconditional_var: float = 2.5e-5) -> pd.DataFrame:
    """A synthetic 15m-bar price path whose true hourly variance follows a
    GARCH(1,1) with Student-t(5) innovations (rescaled to unit variance) --
    OUTSIDE the candidate's log-HAR family, since it feeds back on the
    SQUARED PRIOR SHOCK rather than log-HAR's regression on lagged ln(RV)
    averages at 1h/24h/168h. The same intraday seasonality as
    `seasonal_dgp` (3x multiplicative at 14 UTC weekdays, geometric-mean
    renormalized) is applied on top of the GARCH state, and realized bars
    are built the same way as `seasonal_dgp` (4 Gaussian 15m sub-returns
    per hour, so RV is a noisy but unbiased proxy of the true hourly
    variance) -- the only thing that differs from `seasonal_dgp` is the
    process generating the TRUE variance sequence. The candidate model
    (`seasonal_har`) is fit against this data UNCHANGED.

    - `h_t` = GARCH(1,1) state: `h_t = omega + a*r_{t-1}^2 + b*h_{t-1}`,
      `r_{t-1} = sqrt(h_{t-1}) * z_{t-1}`, `z ~ t(5)` standardized to unit
      variance (`* sqrt(3/5)`, since Var[t_5] = 5/3).
    - `omega = unconditional_var * (1 - a - b)`, so the GARCH state's own
      unconditional mean equals `unconditional_var` -- matched to
      `seasonal_dgp`'s (`exp(HAR_BETA[0]/(1-sum(HAR_BETA[1:]))) ~= 2.5e-5`)
      so `RV_FLOOR` never binds and the two DGPs are comparable in scale.
    - `h_0 = unconditional_var` (the process starts at its own stationary
      mean, so no burn-in bias; realized fluctuation still needs the
      caller's own warm-up years before scoring, same as `seasonal_dgp`).

    A6b-only: never used by stage A/B/C, never touches real data."""
    if a + b >= 1.0:
        raise ValueError(f"a+b={a + b} must be < 1 for a stationary GARCH(1,1)")

    rng = np.random.default_rng(seed)
    n_hours = int(round(years * 365 * 24))
    idx = pd.date_range(start, periods=n_hours, freq="h", tz="UTC")

    s_cells = np.ones(168)
    if seasonal:
        for k in range(168):
            weekday, hour = divmod(k, 24)
            if weekday < 5 and hour == 14:
                s_cells[k] = 3.0
        ln_s_cells = np.log(s_cells)
        ln_s_cells -= ln_s_cells.mean()
        s_cells = np.exp(ln_s_cells)
    how = hour_of_week(idx)
    s = s_cells[how]

    omega = unconditional_var * (1.0 - a - b)
    z = rng.standard_t(5, size=n_hours) * _T5_SCALE
    h_t = np.empty(n_hours)
    h_t[0] = unconditional_var
    for t in range(1, n_hours):
        r_prev = np.sqrt(max(h_t[t - 1], RV_FLOOR)) * z[t - 1]
        h_t[t] = omega + a * r_prev ** 2 + b * h_t[t - 1]

    rv_true = np.maximum(h_t, RV_FLOOR) * s
    sub_std = np.sqrt(rv_true / 4.0)
    sub_returns = rng.normal(0.0, 1.0, size=(n_hours, 4)) * sub_std[:, None]
    log_price = np.concatenate([[0.0], np.cumsum(sub_returns.reshape(-1))])
    closes = np.exp(log_price[1:])

    open_times = pd.date_range(start - pd.Timedelta(minutes=15), periods=n_hours * 4,
                                freq="15min", tz="UTC")
    return pd.DataFrame({
        "timestamp": open_times,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1.0,
    })
