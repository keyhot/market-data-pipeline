"""E15b (i): vol-managed exposure (Bongaerts, Kang & van Dijk 2020, eq. 3,
L_max = 1), verbatim, on BTC/ETH spot. Pure, DB-free (Sprint 9 model-plane
convention): every function here takes plain arrays/frames and returns
plain arrays/frames. The frozen design, every window/threshold/formula and
"Amendment 1" (which supersedes the frozen primary-comparator text) are
pinned in the vault, `Docs/vol-managed-exposure-experiment.md`; this module
applies them, never chooses them.

Timing convention (see the note's "Timing and the rule, exactly"):
- Daily return of day D: r_D = C(D)/C(D-1) - 1, indexed by D (the day's
  OPEN date) -- unlike E16's trend module, there is no close-time shift
  here.
- Month-t decision time T_{t-1} = 00:00 UTC on day 1 of month t, which is
  exactly C(D) for D = the last day of month t-1 (a daily bar's close is
  known at D+1 00:00).
- sigma_hat_{t-1} (real) excludes month t-1's last day; the oracle variant
  (A1 only) uses month t's own days, all of them, same MIN_SIGMA_DAYS floor.
- sigma_target(t) is the expanding std through month t-1's last day
  inclusive, for both the real and oracle exposure paths (Amendment 1 does
  not touch it).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ES_LEVEL = 0.975
ES_REPORT_DAILY = 0.99
ES_REPORT_MONTHLY = 0.99
Q_HIGH = 0.80
Q_LOW = 0.20
L_MAX = 1.0
MIN_SIGMA_DAYS = 15
COST_SIDE = 0.0011
BOOT_BLOCK = 30
BOOT_REPS = 10_000
SEED = 7
LW_LAG = 30
FIRST_RETURN_DAY = "2017-08-18"
FIRST_SCORED = "2020-01"
LAST_SCORED_B = "2025-12"

_Z_95 = 1.959964
_BOOT_BATCH = 500


# --------------------------------------------------------------------------
# Returns / sigma / state / exposure
# --------------------------------------------------------------------------

def daily_returns(close: pd.Series) -> pd.Series:
    """r_D = C(D)/C(D-1) - 1, indexed by D (the day's open date, matching
    `close`'s own index -- no shift)."""
    close = close.sort_index()
    r = close / close.shift(1) - 1.0
    return r.dropna()


def _month_periods(r: pd.Series) -> pd.PeriodIndex:
    return pd.PeriodIndex(pd.DatetimeIndex(r.index), freq="M")


def month_sigma(r: pd.Series) -> pd.Series:
    """sigma_hat per month: std (ddof=1) of the month's daily returns,
    EXCLUDING its last day. NaN if fewer than MIN_SIGMA_DAYS remain."""
    idx = _month_periods(r)
    periods = pd.PeriodIndex(sorted(set(idx)))
    out = {}
    for period in periods:
        vals = r.to_numpy()[idx == period]
        excl_last = vals[:-1] if len(vals) > 0 else vals
        if len(excl_last) < MIN_SIGMA_DAYS:
            out[period] = np.nan
        else:
            out[period] = float(np.std(excl_last, ddof=1))
    return pd.Series(out, dtype=float).sort_index()


def _oracle_month_sigma(r: pd.Series) -> pd.Series:
    """The A1 oracle's sigma_hat: std (ddof=1) of month t's OWN daily
    returns, all days, same MIN_SIGMA_DAYS floor."""
    idx = _month_periods(r)
    periods = pd.PeriodIndex(sorted(set(idx)))
    out = {}
    for period in periods:
        vals = r.to_numpy()[idx == period]
        if len(vals) < MIN_SIGMA_DAYS:
            out[period] = np.nan
        else:
            out[period] = float(np.std(vals, ddof=1))
    return pd.Series(out, dtype=float).sort_index()


def sigma_target(r: pd.Series, month) -> float:
    """Expanding std (ddof=1) of every daily return from the series' start
    through the last day of `month` inclusive, and nothing after."""
    month = pd.Period(month, freq="M")
    idx = _month_periods(r)
    mask = idx <= month
    vals = r.to_numpy()[mask]
    return float(np.std(vals, ddof=1))


def vol_state(sigma_hist: pd.Series, month) -> str:
    """"high"/"low"/"medium" for `month`, from the quintiles of every
    non-NaN sigma_hist value up to and including `month`."""
    month = pd.Period(month, freq="M")
    hist = sigma_hist.loc[sigma_hist.index <= month].dropna()
    q_hi = np.quantile(hist.to_numpy(), Q_HIGH, method="linear")
    q_lo = np.quantile(hist.to_numpy(), Q_LOW, method="linear")
    v = float(sigma_hist.loc[month])
    if v >= q_hi:
        return "high"
    if v <= q_lo:
        return "low"
    return "medium"


def exposure(state: str, s_target: float, s_hat: float, l_max: float = L_MAX) -> float:
    """eq. 3, verbatim: e = min(sigma_target/sigma_hat, l_max) in an
    extreme (high or low) state; e = 1 in a medium state."""
    if state == "medium":
        return 1.0
    return float(min(s_target / s_hat, l_max))


def exposures(r: pd.Series, first, last, oracle: bool = False) -> pd.Series:
    """Monthly target exposure e_t for every month t in [first, last]
    (inclusive), indexed by month (Period[M]).

    Real path (oracle=False): sigma_hat is month t-1's (excl. its last
    day); the state's hist runs through t-1 inclusive.
    Oracle path (A1 only): sigma_hat is month t's OWN (all days); the
    state's hist runs through t inclusive. sigma_target is unchanged in
    both paths -- it is always the expanding std through month t-1's last
    day (Amendment/A1: "the same sigma_target ... apply")."""
    first = pd.Period(first, freq="M")
    last = pd.Period(last, freq="M")
    months = pd.period_range(first, last, freq="M")
    sigma_hat_series = _oracle_month_sigma(r) if oracle else month_sigma(r)

    out = {}
    for t in months:
        ref_month = t if oracle else (t - 1)
        s_hat = float(sigma_hat_series.loc[ref_month])
        state = vol_state(sigma_hat_series, ref_month)
        s_target = sigma_target(r, t - 1)
        out[t] = exposure(state, s_target, s_hat, L_MAX)
    return pd.Series(out, dtype=float)


# --------------------------------------------------------------------------
# Holding (managed_daily is the ONE path both VM and BH/constant books run
# through, so A2's "VM equals BH bitwise" is a real identity, not a
# tolerance -- see model docstring / the vault's A2 row)
# --------------------------------------------------------------------------

def managed_daily(close: pd.Series, e: pd.Series, cost_side: float = COST_SIDE,
                  e_start: float = 1.0) -> pd.DataFrame:
    """Per unit of equity at each month's decision time T_{t-1}: V_D =
    (1-e_t) + e_t*C(D)/C(T_{t-1}), y_D = V_D/V_{D-1} - 1, with the first
    day of the month cost-adjusted: y_first = (1-kappa_t)(1+e_t*r_first)-1,
    kappa_t = cost_side*|e_t - w_prev| (w_prev = the previous month's
    drifted risky weight at its last day, or `e_start` for `e`'s first
    month). `close` must include, for every month in `e`, all of that
    month's days AND the prior month's last day (the anchor).

    Returns a frame indexed by day D with columns `y` (daily book return),
    `kappa` (0 except a month's first day) and `w_end` (the drifted risky
    weight at day D)."""
    idx = _month_periods_from_index(close.index)
    months = pd.PeriodIndex(sorted(e.index))

    y_out: dict = {}
    kappa_out: dict = {}
    w_out: dict = {}
    w_prev = float(e_start)

    for t in months:
        day_mask = idx == t
        days = close.index[day_mask]
        if len(days) == 0:
            raise ValueError(f"managed_daily: no bars for month {t}")
        order = np.argsort(days.values)
        days = days[order]

        prior_mask = idx == (t - 1)
        prior_days = close.index[prior_mask]
        if len(prior_days) == 0:
            raise ValueError(f"managed_daily: no prior-month anchor bar for month {t}")
        p0 = float(close.loc[prior_days.max()])

        e_t = float(e.loc[t])
        prices = close.loc[days].to_numpy(dtype=float)
        V = (1.0 - e_t) + e_t * prices / p0
        V_prev = np.empty_like(V)
        V_prev[0] = 1.0
        V_prev[1:] = V[:-1]
        y = V / V_prev - 1.0

        r_first = prices[0] / p0 - 1.0
        kappa_t = cost_side * abs(e_t - w_prev)
        y[0] = (1.0 - kappa_t) * (1.0 + e_t * r_first) - 1.0

        w_day = e_t * prices / p0 / V

        for d, yy, ww in zip(days, y, w_day):
            y_out[d] = float(yy)
            w_out[d] = float(ww)
        kappa_out[days[0]] = float(kappa_t)
        w_prev = float(w_day[-1])

    y_s = pd.Series(y_out).sort_index()
    kappa_s = pd.Series(kappa_out).reindex(y_s.index, fill_value=0.0)
    w_s = pd.Series(w_out).sort_index()
    return pd.DataFrame({"y": y_s, "kappa": kappa_s, "w_end": w_s})


def _month_periods_from_index(index: pd.Index) -> pd.PeriodIndex:
    return pd.PeriodIndex(pd.DatetimeIndex(index), freq="M")


def constant_exposure_book(close: pd.Series, e_bar: float, first, last,
                          cost_side: float = COST_SIDE, e_start: float = 1.0) -> pd.DataFrame:
    """Amendment 1's control (b): a constant book at exposure `e_bar` over
    [first, last], monthly-rebalanced back to e_bar, same cost profile.
    Runs through the identical `managed_daily` path as the candidate (and
    as buy-and-hold, e_bar=1.0), so A2's bitwise identity holds for real."""
    first = pd.Period(first, freq="M")
    last = pd.Period(last, freq="M")
    months = pd.period_range(first, last, freq="M")
    e = pd.Series(float(e_bar), index=months)
    return managed_daily(close, e, cost_side=cost_side, e_start=e_start)


# Spec name (module docstring table); Amendment 1 renamed the function to
# `constant_exposure_book` -- kept as an alias, logged as a spec gap.
constant_exposure_daily = constant_exposure_book


def permuted_exposures(e: pd.Series, seed: int = SEED) -> pd.Series:
    """The same multiset of monthly exposures, shuffled across the same
    months with `np.random.default_rng(seed)`."""
    rng = np.random.default_rng(seed)
    values = e.to_numpy(dtype=float).copy()
    rng.shuffle(values)
    return pd.Series(values, index=e.index)


# --------------------------------------------------------------------------
# ES / bootstrap / LW Sharpe delta
# --------------------------------------------------------------------------

def expected_shortfall(y: np.ndarray, q: float) -> float:
    """ES_q(y) = -mean(the k smallest y), k = ceil((1-q)*n). Losses are
    positive. `round(..., 9)` before `ceil` guards float noise (e.g.
    (1-0.975)*200 == 5.000000000000004) from inflating k by one."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    k = int(np.ceil(round((1.0 - q) * n, 9)))
    k = max(1, min(k, n))
    smallest = np.sort(y)[:k]
    return float(-smallest.mean())


def delta_es(bh, vm, q: float = ES_LEVEL) -> float:
    """ES_q(bh) - ES_q(vm); positive means vm's tail loss is smaller."""
    return expected_shortfall(bh, q) - expected_shortfall(vm, q)


def paired_block_bootstrap(a, b, stat, block: int = BOOT_BLOCK,
                            reps: int = BOOT_REPS, seed: int = SEED) -> tuple[float, float, float]:
    """Paired circular block bootstrap: each rep resamples ONE set of
    indices and applies it to both `a` and `b`, so pairing (a, a) is
    identity in every rep. Returns (point estimate on the original data,
    CI lower, CI upper) via the (2.5, 97.5) percentiles of the bootstrap
    replicate distribution. Reps run in batches (memory)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n = len(a)
    point = float(stat(a, b))
    rng = np.random.default_rng(seed)
    num_blocks = int(np.ceil(n / block))

    vals = np.empty(reps, dtype=float)
    done = 0
    while done < reps:
        take = min(_BOOT_BATCH, reps - done)
        starts = rng.integers(0, n, size=(take, num_blocks))
        offsets = np.arange(block)
        idx = (starts[:, :, None] + offsets[None, None, :]) % n
        idx = idx.reshape(take, num_blocks * block)[:, :n]
        for i in range(take):
            row = idx[i]
            vals[done + i] = stat(a[row], b[row])
        done += take

    lo, hi = np.percentile(vals, [2.5, 97.5])
    return point, float(lo), float(hi)


def lw_sharpe_diff(r1, r2, lag: int = LW_LAG) -> tuple[float, float, float]:
    """Ledoit & Wolf 2008 (Sec. 3.1) HAC delta-method CI for the Sharpe
    difference f(u) = mu1/sqrt(g1-mu1^2) - mu2/sqrt(g2-mu2^2), u = (mu1,
    mu2, g1, g2) with g = E[r^2]. Psi_hat is the Newey-West (Bartlett,
    `lag`) covariance of (r1-mu1, r2-mu2, r1^2-g1, r2^2-g2). Returns
    (delta, ci_lo, ci_hi) at the 95% level, NOT annualised (the runner
    annualises by sqrt(365) for the report-only figure)."""
    r1 = np.asarray(r1, dtype=float)
    r2 = np.asarray(r2, dtype=float)
    n = len(r1)
    mu1, mu2 = float(r1.mean()), float(r2.mean())
    g1, g2 = float((r1 ** 2).mean()), float((r2 ** 2).mean())
    s1 = g1 - mu1 ** 2
    s2 = g2 - mu2 ** 2
    delta = mu1 / np.sqrt(s1) - mu2 / np.sqrt(s2)

    grad = np.array([
        g1 / s1 ** 1.5,
        -g2 / s2 ** 1.5,
        -mu1 / (2 * s1 ** 1.5),
        mu2 / (2 * s2 ** 1.5),
    ])

    d = np.column_stack([r1 - mu1, r2 - mu2, r1 ** 2 - g1, r2 ** 2 - g2])
    Psi = (d.T @ d) / n
    max_lag = min(lag, n - 1)
    for k in range(1, max_lag + 1):
        gk = (d[k:].T @ d[:-k]) / n
        w = 1.0 - k / (lag + 1)
        Psi = Psi + w * (gk + gk.T)

    var = float(grad @ Psi @ grad) / n
    se = np.sqrt(var) if var > 0 else 0.0
    return float(delta), float(delta - _Z_95 * se), float(delta + _Z_95 * se)


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def symbol_pass(ci_lo_bh: float, ci_lo_ebar: float) -> bool:
    """Amendment 1's conjunction: PASS only if BOTH the vs-BH and the
    vs-exposure-matched-book ES-reduction CI lower bounds are > 0. This
    supersedes the frozen note's single-argument `symbol_pass(ci_lo)` --
    logged as a spec gap."""
    return bool(ci_lo_bh > 0 and ci_lo_ebar > 0)


def overall_verdict(passes: dict[str, bool]) -> str:
    n_pass = sum(1 for v in passes.values() if v)
    if n_pass == len(passes) and n_pass > 0:
        return "PASS"
    if n_pass == 0:
        return "FAIL"
    return "SPLIT"


def stage_c_verdict(delta: float) -> str:
    return "CONFIRMED" if delta > 0 else "NOT_CONFIRMED"


def stage_a_valid(checks: dict[str, bool]) -> tuple[bool, list[str]]:
    failing = [k for k, v in checks.items() if not v]
    return (len(failing) == 0, failing)
