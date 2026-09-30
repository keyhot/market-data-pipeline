"""E16 trend: Hurst, Ooi & Pedersen 2017 (HOP) monthly TSMOM on a pooled
BTC + ETH book, long/short. Pure, DB-free (Sprint 9 model-plane convention):
every function here takes plain arrays/frames/timestamps and returns plain
arrays/frames. The frozen design is `Docs/trend-experiment.md` in the vault;
every constant, window and formula below is pinned there and MUST NOT be
changed without a dated amendment to that note.

Timestamp convention (fixed here, read before touching any function): every
daily price series this module receives is indexed by CLOSE time (a bar
opening at day D 00:00 UTC closes at D+1 00:00) -- the loaders in
`scripts/trend_report.py` do that shift once, so nothing here re-derives it.
`T_m`, wherever it appears, is 00:00 UTC on the first day of month m+1 (the
close of the bar opening on the last day of month m) -- i.e. every monthly
series produced here is indexed on a month-start (`MS`) grid whose entries
ARE the T_m timestamps directly, so "T_{m-1}" is simply the previous row.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from model.backtest import COST_PROFILES

LOOKBACKS = (1, 3, 12)
EWMA_COM = 60
ANNUAL_DAYS = 365
TARGET_VOL = 0.10
COV_MONTHS = 36
GROSS_CAP = 1.0
COST_SIDE_PERP = round(
    COST_PROFILES["perp_taker"].fee_per_side + COST_PROFILES["perp_taker"].slippage, 10
)
COST_SIDE_SPOT = round(
    COST_PROFILES["spot_taker"].fee_per_side + COST_PROFILES["spot_taker"].slippage, 10
)
BOOT_BLOCK = 6
BOOT_REPS = 10_000
SEED = 7
EXPECTED_IR = 0.20
FIRST_HOLD = "2020-09"
LAST_HOLD_B = "2025-12"
FIRST_HOLD_C = "2026-01"
LAST_HOLD_C = "2026-08"


# --------------------------------------------------------------------------
# Monthly grid
# --------------------------------------------------------------------------

def month_end_closes(daily_close: pd.Series) -> pd.Series:
    """`daily_close` indexed by CLOSE time -> a Series on the month-start
    (T_m) grid spanning its own range, valued at the close price AT that
    exact timestamp. A month whose 1st-of-next-month timestamp is not
    present in `daily_close` (before the series starts, or a genuine gap)
    is NaN rather than filled -- callers must not read past a NaN month."""
    idx = pd.DatetimeIndex(daily_close.index)
    start = pd.Timestamp(year=idx.min().year, month=idx.min().month, day=1, tz="UTC")
    end = pd.Timestamp(year=idx.max().year, month=idx.max().month, day=1, tz="UTC")
    grid = pd.date_range(start, end, freq="MS", tz="UTC")
    return daily_close.reindex(grid)


def monthly_returns(month_closes: pd.DataFrame) -> pd.DataFrame:
    """R_{i,m} = C_i(T_m)/C_i(T_{m-1}) - 1, indexed by T_m (the same grid
    `month_end_closes` returns). The first row is NaN (no T_{m-1})."""
    return month_closes / month_closes.shift(1) - 1.0


def signals(month_closes: pd.DataFrame, T) -> pd.DataFrame:
    """s_{i,k} for k in LOOKBACKS: sign of C_i(T)/C_i(T - k months) - 1,
    tie (exact 0) -> +1. Rows = symbols (month_closes' columns), columns =
    LOOKBACKS. Reads only rows at T and T - k months -- never anything
    after T, whatever else `month_closes` contains."""
    T = pd.Timestamp(T)
    cols = {}
    c_t = month_closes.loc[T]
    for k in LOOKBACKS:
        t_lag = T - pd.DateOffset(months=k)
        c_lag = month_closes.loc[t_lag]
        r = c_t / c_lag - 1.0
        cols[k] = np.where(r.to_numpy() >= 0, 1.0, -1.0)
    return pd.DataFrame(cols, index=month_closes.columns)


# --------------------------------------------------------------------------
# MOP (2012) per-position vol estimator
# --------------------------------------------------------------------------

def mop_vol(daily_close: pd.Series, T, annual_days: int = ANNUAL_DAYS) -> float:
    """sigma_hat_i(T) = sqrt(annual_days * v), v = the EWMA (com=60,
    adjust=True, bias=True) variance of daily simple returns with close
    time <= T. Truncates internally, so passing a longer series than
    strictly needed can never leak a return that closes after T."""
    T = pd.Timestamp(T)
    close = daily_close.loc[daily_close.index <= T]
    r = close.pct_change().dropna()
    if len(r) < 2:
        return float("nan")
    v = float(r.ewm(com=EWMA_COM, adjust=True).var(bias=True).iloc[-1])
    return float(np.sqrt(annual_days * v))


# --------------------------------------------------------------------------
# HOP portfolio scaling (fn. 7)
# --------------------------------------------------------------------------

def hop_exposures(
    S: pd.Series, sigma: pd.Series, cov: np.ndarray,
    target: float = TARGET_VOL, cap: float = GROSS_CAP,
) -> tuple[pd.Series, bool]:
    """W = S / sigma; lambda = target / sqrt(12 * W' Cov W); e = lambda * W,
    proportionally rescaled to gross <= cap. `S`, `sigma` and `cov`'s rows
    /columns must already share ONE fixed symbol order -- the caller's job,
    never guessed here."""
    symbols = list(S.index)
    W = (S / sigma).to_numpy()
    quad = float(W @ cov @ W)
    if quad <= 0:
        raise ValueError(f"W'Cov W = {quad} <= 0; portfolio variance is non-positive")
    lam = target / np.sqrt(12.0 * quad)
    e = pd.Series(lam * W, index=symbols)
    gross = float(e.abs().sum())
    capped = gross > cap
    if capped:
        e = e / gross * cap
    return e, capped


def rebalance_exposures(
    spot_daily: pd.DataFrame, T, signal: str = "trend", override_S: pd.Series | None = None,
) -> tuple[pd.Series, bool]:
    """One rebalance at T_m: signal -> S, MOP vol -> sigma, the trailing
    36-month sample covariance of monthly spot returns -> cov, HOP fn. 7 ->
    (e, capped). `signal` in {"trend", "plus_one"}; `override_S` (used only
    by stage A's oracle/anti-oracle) replaces S entirely, still computing
    sigma/cov/lambda/cap normally."""
    T = pd.Timestamp(T)
    symbols = list(spot_daily.columns)
    month_closes = pd.DataFrame(
        {s: month_end_closes(spot_daily[s]) for s in symbols}
    )[symbols]

    if override_S is not None:
        S = override_S.reindex(symbols)
    elif signal == "trend":
        sig = signals(month_closes, T)
        S = sig.mean(axis=1).reindex(symbols)
    elif signal == "plus_one":
        S = pd.Series(1.0, index=symbols)
    else:
        raise ValueError(f"unknown signal {signal!r}")

    sigma = pd.Series({s: mop_vol(spot_daily[s], T) for s in symbols}).reindex(symbols)

    R = monthly_returns(month_closes)
    window_start = T - pd.DateOffset(months=COV_MONTHS - 1)
    window = R.loc[(R.index >= window_start) & (R.index <= T), symbols]
    window = window.dropna()
    if len(window) != COV_MONTHS:
        raise ValueError(
            f"covariance window at {T} has {len(window)} valid monthly rows, "
            f"need {COV_MONTHS}"
        )
    cov = np.cov(window.to_numpy(), rowvar=False, ddof=1)

    return hop_exposures(S, sigma, cov)


# --------------------------------------------------------------------------
# Returns, costs and funding
# --------------------------------------------------------------------------

def _monthly_sum(daily: pd.Series) -> pd.Series:
    """Sum of a daily series' calendar-month buckets, labelled on the SAME
    T_m grid `month_end_closes` uses (i.e. a bucket for calendar month m is
    labelled T_m = the 1st of month m+1, not month m's own 1st)."""
    monthly = daily.resample("MS").sum()
    monthly.index = monthly.index + pd.DateOffset(months=1)
    return monthly


def holding_excess_returns(
    perp_close: pd.DataFrame, funding_daily: pd.DataFrame
) -> pd.DataFrame:
    """r~_{i,h} = P_i(T_h)/P_i(T_{h-1}) - 1 - F_{i,h}, indexed by T_h (the
    same month grid). `perp_close` is daily, close-time indexed; `funding_
    daily` is `model.cross_section.funding_by_day`'s per-holding-day sums,
    one column per symbol."""
    symbols = list(perp_close.columns)
    month_closes = pd.DataFrame({s: month_end_closes(perp_close[s]) for s in symbols})
    perp_r = monthly_returns(month_closes)[symbols]
    funding_monthly = pd.DataFrame({s: _monthly_sum(funding_daily[s]) for s in symbols})[symbols]
    common = perp_r.index.intersection(funding_monthly.index)
    return (perp_r.loc[common] - funding_monthly.loc[common])[symbols]


def book_returns(
    e: pd.DataFrame, r_tilde: pd.DataFrame, price_ret: pd.DataFrame, cost_side: float,
) -> pd.DataFrame:
    """Sequential month-by-month accounting. Columns: G (gross), kappa
    (turnover cost), R (net = G - kappa), and kappa_<symbol> per symbol.
    The book starts flat (e_prev = 0), so the first scored month is charged
    its full entry. `price_ret` is the PERP PRICE return (no funding) --
    funding is cash, never notional drift."""
    symbols = list(e.columns)
    idx = e.index
    e = e[symbols]
    r_tilde = r_tilde.reindex(idx)[symbols]
    price_ret = price_ret.reindex(idx)[symbols]

    rows = []
    e_prev = pd.Series(0.0, index=symbols)
    pr_prev = pd.Series(0.0, index=symbols)
    n_prev = 0.0
    for t in idx:
        e_drift = e_prev * (1.0 + pr_prev) / (1.0 + n_prev)
        turnover = (e.loc[t] - e_drift).abs()
        kappa_i = cost_side * turnover
        kappa = float(kappa_i.sum())
        g = float((e.loc[t] * r_tilde.loc[t]).sum())
        r = g - kappa
        row = {"G": g, "kappa": kappa, "R": r}
        for s in symbols:
            row[f"kappa_{s}"] = float(kappa_i[s])
        rows.append(row)
        e_prev = e.loc[t]
        pr_prev = price_ret.loc[t]
        n_prev = r
    return pd.DataFrame(rows, index=idx)


def signed_null(
    e: pd.DataFrame, r_tilde: pd.DataFrame, kappa: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """N^b_h = ebar . r~_h - kappa^b_h (the book's OWN cost, reused, so
    costs cancel exactly against R^b_h in `excess_over_null`). ebar is the
    mean signed exposure over `e`'s own (already stage-windowed) rows."""
    symbols = list(e.columns)
    e_bar = e[symbols].mean(axis=0)
    n = (r_tilde.reindex(e.index)[symbols] * e_bar).sum(axis=1) - kappa.reindex(e.index)
    return n, e_bar


def excess_over_null(e: pd.DataFrame, r_tilde: pd.DataFrame) -> pd.Series:
    """x_h = sum_i (e_{i,h} - ebar_i) r~_{i,h}. Costs never enter: this is
    exactly R_h - N_h algebraically (both charge the same kappa_h), which is
    also what `signed_null`'s bitwise contract with `book_returns` rests on."""
    symbols = list(e.columns)
    e_bar = e[symbols].mean(axis=0)
    return ((e[symbols] - e_bar) * r_tilde.reindex(e.index)[symbols]).sum(axis=1)


def signed_exposure_matched_return(
    exposure: float, benchmark_return: float, positions: int, cost: float
) -> float:
    """The no-skill total-return form: a static book at the average SIGNED
    exposure, charged one round trip per position. For exposure >= 0 this
    is bitwise `model.benchmark.exposure_matched_return`."""
    return (1.0 + exposure * benchmark_return) * (1.0 - cost) ** positions - 1.0


# --------------------------------------------------------------------------
# Verdict conditions
# --------------------------------------------------------------------------

def conditions(R_c: pd.Series, N_c: pd.Series, R_plus: pd.Series) -> tuple[pd.Series, pd.Series]:
    """x = R_c - N_c (condition 1's reference is the signed-exposure null);
    d = R_c - R_plus (condition 2's reference is the +1 control). The ONE
    place the two references are picked -- `primary_verdict` never sees R_c,
    N_c or R_plus directly, only what this returns (KI-094)."""
    x = R_c - N_c
    d = R_c - R_plus
    return x, d


def primary_verdict(mean_x: float, mean_d: float) -> str:
    return "PASS" if (mean_x > 0 and mean_d > 0) else "FAIL"


def pass_95(ci_lo_x: float, ci_lo_d: float) -> bool:
    return bool(ci_lo_x > 0 and ci_lo_d > 0)


def stage_c_verdict(mean_x: float, mean_d: float) -> str:
    return "CONFIRMED" if (mean_x > 0 and mean_d > 0) else "NOT_CONFIRMED"


def stage_a_valid(checks: dict[str, bool]) -> tuple[bool, list[str]]:
    failing = [k for k, v in checks.items() if not v]
    return (len(failing) == 0, failing)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def annualised_ir(series) -> float:
    x = np.asarray(series, dtype=float)
    sd = x.std(ddof=1)
    if sd == 0 or len(x) < 2:
        return float("nan")
    return float(x.mean() / sd * np.sqrt(12))


def block_bootstrap_ci(
    series, stat=np.mean, block: int = BOOT_BLOCK, reps: int = BOOT_REPS,
    seed: int = SEED, alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Circular moving-block bootstrap (point, lo, hi) of `stat` (default
    mean) at (1 - alpha) coverage, percentile method. Resampled indices
    wrap circularly (mod n), so every start in [0, n) is usable, including
    ones within `block` of the end."""
    x = np.asarray(series, dtype=float)
    n = len(x)
    point = float(stat(x))
    if n == 0:
        return point, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(reps, n_blocks))
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    idx = idx.reshape(reps, n_blocks * block)[:, :n]
    samples = x[idx]
    stats = stat(samples, axis=1)
    lo, hi = np.quantile(stats, [alpha / 2.0, 1.0 - alpha / 2.0])
    return point, float(lo), float(hi)
