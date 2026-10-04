"""E15 range forecast: seasonal log-HAR + filtered historical simulation
(FHS) 80% band on hourly (and 4h) log returns, walk-forward, calibrated by
Christoffersen UC/CC and Engle-Manganelli DQ, sharpened vs plain HAR by the
Winkler interval score under a Newey-West HAC CI.

Pure, DB-free (Sprint 9 model-plane convention): every function here takes
plain arrays/frames and returns plain arrays/frames. The frozen design is
`Docs/range-forecast-experiment.md` in the vault; every constant, window
and formula below is pinned there and MUST NOT be changed without a dated
amendment to that note.

This module also carries `seasonal_dgp`, a synthetic price-path generator
used ONLY by the unit tests and stage A's A6 (never on real data) so both
consumers share exactly one definition of "a near-correct seasonal HAR
world" (the repo's one-definition convention, e.g. `find_episodes`,
`is_degraded_stream_event`).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.special import xlogy
from scipy.stats import chi2

ALPHA_PRIMARY = 0.20
ALPHA_REPORT = 0.05
HAC_LAG = 168
RV_FLOOR = 1e-8
FACTOR_WINDOW_H = 8736
ROLL_WINDOW_H = 168
MIN_VALID = {1: 1, 24: 12, 168: 84}
MIN_CELL = 40
P_LEVEL = 0.05
DQ_HOURS = (0, 8, 16, 14)
DQ_LAGS = 4

_HAR_BETA = np.array([-0.5298, 0.40, 0.35, 0.20])  # DGP-only; see seasonal_dgp
_Z_95 = 1.959964


# --------------------------------------------------------------------------
# Timing / grid
# --------------------------------------------------------------------------

def hour_of_week(ts: pd.DatetimeIndex) -> np.ndarray:
    """`24*weekday + hour`, UTC, Monday = 0, range 0..167."""
    ts = pd.DatetimeIndex(ts)
    return (24 * ts.dayofweek + ts.hour).to_numpy()


def hourly_grid(bars_15m: pd.DataFrame) -> pd.DataFrame:
    """15m bars (columns `timestamp`/index, `close`, `volume`) -> an hourly
    grid indexed at UTC hour boundaries t with columns `close_t` (C(t)),
    `rv`, `r1`, `valid`, `how`. A bar with `volume == 0` is treated as
    missing. See the note's Timing rule for the exact five-bar window."""
    if "timestamp" in bars_15m.columns:
        idx = pd.DatetimeIndex(bars_15m["timestamp"])
    else:
        idx = pd.DatetimeIndex(bars_15m.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")

    close = pd.Series(bars_15m["close"].to_numpy(dtype=float), index=idx)
    volume = pd.Series(bars_15m["volume"].to_numpy(dtype=float), index=idx)
    close = close[~close.index.duplicated(keep="last")].sort_index()
    volume = volume[~volume.index.duplicated(keep="last")].sort_index()

    if len(close) == 0:
        return pd.DataFrame(columns=["close_t", "rv", "r1", "valid", "how"])

    boundary = close.index + pd.Timedelta(minutes=15)
    close_by_boundary = pd.Series(close.to_numpy(), index=boundary)
    ok_by_boundary = pd.Series(volume.to_numpy() > 0, index=boundary)

    full = pd.date_range(boundary.min(), boundary.max(), freq="15min", tz="UTC")
    c = close_by_boundary.reindex(full)
    ok = ok_by_boundary.reindex(full, fill_value=False)

    c0, c1, c2, c3, c4 = c, c.shift(-1), c.shift(-2), c.shift(-3), c.shift(-4)
    ok0 = ok
    ok1 = ok.shift(-1, fill_value=False)
    ok2 = ok.shift(-2, fill_value=False)
    ok3 = ok.shift(-3, fill_value=False)
    ok4 = ok.shift(-4, fill_value=False)

    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = np.log(c1.to_numpy() / c0.to_numpy())
        d2 = np.log(c2.to_numpy() / c1.to_numpy())
        d3 = np.log(c3.to_numpy() / c2.to_numpy())
        d4 = np.log(c4.to_numpy() / c3.to_numpy())
        r1_raw = np.log(c4.to_numpy() / c0.to_numpy())

    valid = (
        ok0.to_numpy(dtype=bool) & ok1.to_numpy(dtype=bool)
        & ok2.to_numpy(dtype=bool) & ok3.to_numpy(dtype=bool)
        & ok4.to_numpy(dtype=bool)
        & c0.notna().to_numpy() & c1.notna().to_numpy()
        & c2.notna().to_numpy() & c3.notna().to_numpy() & c4.notna().to_numpy()
    )

    rv_raw = d1 ** 2 + d2 ** 2 + d3 ** 2 + d4 ** 2
    rv = np.where(valid, np.maximum(rv_raw, RV_FLOOR), np.nan)
    r1 = np.where(valid, r1_raw, np.nan)

    grid = pd.DataFrame(
        {"close_t": c0.to_numpy(), "rv": rv, "r1": r1, "valid": valid},
        index=full,
    )
    grid = grid[grid.index.minute == 0].copy()
    grid["how"] = hour_of_week(grid.index)
    return grid


# --------------------------------------------------------------------------
# Seasonal factor
# --------------------------------------------------------------------------

def seasonal_factor(grid: pd.DataFrame, T: pd.Timestamp) -> np.ndarray:
    """168-cell hour-of-week multiplicative factor (geometric mean 1),
    fit on the valid hours in the trailing 52-week window [T-8736h, T).
    Raises ValueError if any of the 168 cells has fewer than MIN_CELL
    valid hours -- fail loudly, never fill."""
    T = pd.Timestamp(T)
    start = T - pd.Timedelta(hours=FACTOR_WINDOW_H)
    window = grid.loc[(grid.index >= start) & (grid.index < T) & grid["valid"]]
    log_rv = np.log(window["rv"].to_numpy())
    how = window["how"].to_numpy()

    m = np.full(168, np.nan)
    for k in range(168):
        cell = log_rv[how == k]
        if cell.size < MIN_CELL:
            raise ValueError(
                f"hour-of-week cell {k} has {cell.size} valid hours "
                f"(< MIN_CELL={MIN_CELL}) in factor window ending {T}"
            )
        m[k] = cell.mean()
    ln_s = m - m.mean()
    return np.exp(ln_s)


# --------------------------------------------------------------------------
# HAR features / target
# --------------------------------------------------------------------------

def har_features(rv_star: pd.Series, valid: pd.Series) -> pd.DataFrame:
    """x1 = ln RV*_{t-1h} (must be valid); x24 = ln mean(RV*) over valid
    hours in [t-24h, t), >=12/24 valid; x168 likewise over [t-168h, t),
    >=84/168 valid. NaN wherever a requirement fails."""
    shifted_rv = rv_star.shift(1)
    shifted_valid = valid.shift(1).astype("boolean").fillna(False).astype(bool)
    masked = shifted_rv.where(shifted_valid)

    x1 = np.log(masked)

    count24 = shifted_valid.rolling(24, min_periods=1).sum()
    mean24 = masked.rolling(24, min_periods=1).mean()
    x24 = np.log(mean24).where(count24 >= MIN_VALID[24])

    count168 = shifted_valid.rolling(168, min_periods=1).sum()
    mean168 = masked.rolling(168, min_periods=1).mean()
    x168 = np.log(mean168).where(count168 >= MIN_VALID[168])

    return pd.DataFrame({"x1": x1, "x24": x24, "x168": x168}, index=rv_star.index)


def har_target(rv_star: pd.Series, valid: pd.Series, h: int) -> pd.Series:
    """y_{t,h} = ln(mean_{j=0}^{h-1} RV*_{t+j}); NaN unless hours t..t+h-1
    are all valid."""
    fwd_mean = rv_star.rolling(h).mean().shift(-(h - 1))
    fwd_count = valid.astype(int).rolling(h).sum().shift(-(h - 1))
    y = np.log(fwd_mean)
    return y.where(fwd_count == h)


def fit_ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


# --------------------------------------------------------------------------
# Model V / c / sigma^2 (internal core shared by forecast_variance and
# walk_forward, so the FHS training-row sigma used for z and the test-month
# sigma used for the band are provably the same computation).
# --------------------------------------------------------------------------

def _target_rv_sum(grid: pd.DataFrame, h: int) -> pd.Series:
    valid_count = grid["valid"].astype(int).rolling(h).sum().shift(-(h - 1))
    rv_sum = grid["rv"].rolling(h).sum().shift(-(h - 1))
    return rv_sum.where(valid_count == h)


def _realized_return(grid: pd.DataFrame, h: int) -> pd.Series:
    c_t = grid["close_t"]
    c_th = grid["close_t"].shift(-h)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(c_th.to_numpy()) - np.log(c_t.to_numpy())
    return pd.Series(r, index=grid.index)


def _rv_star(grid: pd.DataFrame, factor: np.ndarray | None) -> pd.Series:
    if factor is None:
        return grid["rv"]
    return grid["rv"] / factor[grid["how"].to_numpy()]


def _s_sum_series(index: pd.DatetimeIndex, h: int, factor: np.ndarray | None) -> pd.Series:
    if factor is None:
        return pd.Series(float(h), index=index)
    total = np.zeros(len(index))
    for j in range(h):
        total += factor[hour_of_week(index + pd.Timedelta(hours=j))]
    return pd.Series(total, index=index)


def _fold_core(model: str, grid: pd.DataFrame, T: pd.Timestamp, h: int,
                factor: np.ndarray | None, extra: pd.Series | None = None):
    """One refit's worth of work: fit (beta, c) on the purged/expanding and
    trailing-52wk windows respectively, and return sigma^2 = c*V for every
    row of `grid` (not just the test month), plus the standardized FHS
    training residuals z for this fold.

    `extra` (E17 only; None = E15 exactly): ONE additional OLS regressor per
    origin, e.g. DVOL's x_iv, appended after x168. An origin whose `extra`
    is NaN has no forecast and is never a training row. Valid only for the
    two HAR models."""
    T = pd.Timestamp(T)
    if extra is not None and model not in ("seasonal_har", "plain_har"):
        raise ValueError(f"extra regressor is only defined for HAR models, not {model!r}")
    train_end = T - pd.Timedelta(hours=h)
    fhs_start = max(T - pd.Timedelta(hours=FACTOR_WINDOW_H), grid.index.min())

    target_rv = _target_rv_sum(grid, h)
    realized_r = _realized_return(grid, h)

    if model in ("seasonal_har", "plain_har"):
        s_arr = factor if model == "seasonal_har" else None
        rv_star = _rv_star(grid, s_arr)
        feats = har_features(rv_star, grid["valid"])
        y = har_target(rv_star, grid["valid"], h)
        cols = [
            np.ones(len(grid)), feats["x1"].to_numpy(),
            feats["x24"].to_numpy(), feats["x168"].to_numpy(),
        ]
        if extra is not None:
            ex = pd.Series(extra).reindex(grid.index)
            feats = feats.assign(x_extra=ex.to_numpy(dtype=float))
            cols.append(feats["x_extra"].to_numpy())
        X_all = np.column_stack(cols)
        # A forecast for origin t needs only its FEATURES (backward-looking,
        # available the moment t arrives); a TRAINING row additionally needs
        # its TARGET (forward-looking h hours). Conflating the two made a
        # live forecast at h>1 wrongly depend on data not yet known at t --
        # caught by A3's truncation-invariance check (KI: the fix here).
        feat_ok = feats.notna().all(axis=1)
        train_mask = (feat_ok & y.notna() & (grid.index <= train_end)).to_numpy()
        if train_mask.sum() < 4:
            raise ValueError(
                f"fold {T}: only {train_mask.sum()} training rows for OLS"
            )
        beta = fit_ols(X_all[train_mask], y.to_numpy()[train_mask])
        y_hat = X_all @ beta
        s_sum = _s_sum_series(grid.index, h, s_arr)
        V = s_sum * np.exp(y_hat)
        V = V.where(feat_ok)
        row_ok = feat_ok
    elif model == "rolling":
        count = grid["valid"].astype(int).rolling(ROLL_WINDOW_H, min_periods=1).sum()
        mean_rv = grid["rv"].where(grid["valid"]).rolling(ROLL_WINDOW_H, min_periods=1).mean()
        V = (h * mean_rv).where(count >= MIN_VALID[168])
        row_ok = V.notna()
    elif model == "oracle":
        V = target_rv
        row_ok = V.notna()
    else:
        raise ValueError(f"unknown model {model!r}")

    if model == "oracle":
        c = 1.0
    else:
        fhs_mask = (
            row_ok & (grid.index >= fhs_start) & (grid.index <= train_end)
            & target_rv.notna()
        ).to_numpy()
        if fhs_mask.sum() == 0:
            c = np.nan
        else:
            c = float((target_rv[fhs_mask] / V[fhs_mask]).mean())

    sigma2_all = c * V

    fhs_mask_z = (
        row_ok & (grid.index >= fhs_start) & (grid.index <= train_end)
        & target_rv.notna() & realized_r.notna()
    ).to_numpy()
    denom = np.sqrt(sigma2_all[fhs_mask_z].to_numpy())
    z_train = realized_r[fhs_mask_z].to_numpy() / denom

    return sigma2_all, c, row_ok, target_rv, realized_r, z_train


def forecast_variance(model: str, grid: pd.DataFrame, T, h: int,
                       factor: np.ndarray | None = None) -> pd.Series:
    """c*V for the test-month origins [T, T+1 month); NaN where this
    fold's model has no forecast for that origin."""
    T = pd.Timestamp(T)
    test_end = T + pd.DateOffset(months=1)
    sigma2_all, *_ = _fold_core(model, grid, T, h, factor)
    mask = (grid.index >= T) & (grid.index < test_end)
    return sigma2_all[mask]


# --------------------------------------------------------------------------
# FHS band
# --------------------------------------------------------------------------

def fhs_quantiles(z: np.ndarray, alpha: float) -> tuple[float, float]:
    z = np.asarray(z, dtype=float)
    q_lo = float(np.quantile(z, alpha / 2, method="linear"))
    q_hi = float(np.quantile(z, 1 - alpha / 2, method="linear"))
    return q_lo, q_hi


def band(sigma: np.ndarray, q_lo: float, q_hi: float) -> tuple[np.ndarray, np.ndarray]:
    sigma = np.asarray(sigma, dtype=float)
    return q_lo * sigma, q_hi * sigma


def miss(r, L, U) -> np.ndarray:
    r = np.asarray(r, dtype=float)
    L = np.asarray(L, dtype=float)
    U = np.asarray(U, dtype=float)
    return ((r < L) | (r > U)).astype(int)


# --------------------------------------------------------------------------
# Walk-forward
# --------------------------------------------------------------------------

def walk_forward(grid: pd.DataFrame, h: int, model: str, start, end,
                 extra: pd.Series | None = None) -> pd.DataFrame:
    """Monthly-refit walk-forward over [start, end). Per scored origin:
    sigma, L80, U80, L95, U95, r, rv_target. A row is scored only if this
    model has both a target and a forecast for it."""
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    folds = pd.date_range(start, end, freq="MS", inclusive="left")
    out_rows = []
    for T in folds:
        test_end = T + pd.DateOffset(months=1)
        factor = seasonal_factor(grid, T) if model == "seasonal_har" else None
        sigma2_all, c, row_ok, target_rv, realized_r, z_train = _fold_core(
            model, grid, T, h, factor, extra
        )
        month_mask = (grid.index >= T) & (grid.index < test_end)
        scored_mask = (
            month_mask & row_ok.to_numpy() & target_rv.notna().to_numpy()
            & realized_r.notna().to_numpy()
        )
        idx = grid.index[scored_mask]
        if len(idx) == 0 or len(z_train) == 0:
            continue
        sigma = np.sqrt(sigma2_all[scored_mask].to_numpy())
        q_lo80, q_hi80 = fhs_quantiles(z_train, ALPHA_PRIMARY)
        q_lo95, q_hi95 = fhs_quantiles(z_train, ALPHA_REPORT)
        L80, U80 = band(sigma, q_lo80, q_hi80)
        L95, U95 = band(sigma, q_lo95, q_hi95)
        out_rows.append(pd.DataFrame({
            "sigma": sigma, "L80": L80, "U80": U80, "L95": L95, "U95": U95,
            "r": realized_r[scored_mask].to_numpy(),
            "rv_target": target_rv[scored_mask].to_numpy(),
        }, index=idx))
    if not out_rows:
        return pd.DataFrame(columns=["sigma", "L80", "U80", "L95", "U95", "r", "rv_target"])
    return pd.concat(out_rows).sort_index()


# --------------------------------------------------------------------------
# Scores
# --------------------------------------------------------------------------

def winkler(L, U, y, alpha: float) -> np.ndarray:
    L = np.asarray(L, dtype=float)
    U = np.asarray(U, dtype=float)
    y = np.asarray(y, dtype=float)
    below = y < L
    above = y > U
    s = (U - L) + (2.0 / alpha) * (L - y) * below + (2.0 / alpha) * (y - U) * above
    return s


def qlike(rv, var_hat) -> np.ndarray:
    ratio = np.asarray(rv, dtype=float) / np.asarray(var_hat, dtype=float)
    return ratio - np.log(ratio) - 1.0


# --------------------------------------------------------------------------
# Calibration tests
# --------------------------------------------------------------------------

def lr_uc(hits, alpha: float) -> tuple[float, float]:
    hits = np.asarray(hits)
    n = len(hits)
    n1 = int(hits.sum())
    n0 = n - n1
    pi_hat = n1 / n if n else 0.0
    ll_null = xlogy(n0, 1 - alpha) + xlogy(n1, alpha)
    ll_alt = xlogy(n0, 1 - pi_hat) + xlogy(n1, pi_hat)
    stat = -2.0 * ll_null + 2.0 * ll_alt
    return float(stat), float(chi2.sf(stat, 1))


def lr_ind(hits) -> tuple[float, float]:
    hits = np.asarray(hits)
    prev, curr = hits[:-1], hits[1:]
    n00 = int(((prev == 0) & (curr == 0)).sum())
    n01 = int(((prev == 0) & (curr == 1)).sum())
    n10 = int(((prev == 1) & (curr == 0)).sum())
    n11 = int(((prev == 1) & (curr == 1)).sum())
    n = n00 + n01 + n10 + n11
    pi01 = n01 / (n00 + n01) if (n00 + n01) else 0.0
    pi11 = n11 / (n10 + n11) if (n10 + n11) else 0.0
    pi = (n01 + n11) / n if n else 0.0
    ll_null = xlogy(n00 + n10, 1 - pi) + xlogy(n01 + n11, pi)
    ll_alt = (xlogy(n00, 1 - pi01) + xlogy(n01, pi01)
              + xlogy(n10, 1 - pi11) + xlogy(n11, pi11))
    stat = -2.0 * ll_null + 2.0 * ll_alt
    return float(stat), float(chi2.sf(stat, 1))


def lr_cc(hits, alpha: float) -> tuple[float, float]:
    hits = np.asarray(hits)
    stat_uc_prime, _ = lr_uc(hits[1:], alpha)
    stat_ind, _ = lr_ind(hits)
    stat = stat_uc_prime + stat_ind
    return float(stat), float(chi2.sf(stat, 2))


def _dq_indicator_matrix(hours: np.ndarray, h: int) -> np.ndarray:
    if h == 1:
        mapping = {0: 0, 8: 1, 16: 2, 14: 3}
    elif h == 4:
        mapping = {0: 0, 8: 1, 16: 2, 12: 3}
    else:
        raise ValueError(f"unsupported horizon {h}")
    hours = np.asarray(hours)
    d = np.zeros((len(hours), 4))
    for hour_val, col in mapping.items():
        d[hours == hour_val, col] = 1.0
    return d


def dq_design(origins: pd.DatetimeIndex, hits, alpha: float, h: int):
    """(X, Hit) for the DQ regression, after dropping any origin whose 4
    same-sequence lagged origins (t - k*h, k=1..4) are not themselves in
    the scored set. Looked up by timestamp (via reindex, never by row
    position), so a gap in the sequence never silently bridges."""
    origins = pd.DatetimeIndex(origins)
    hits = np.asarray(hits, dtype=float)
    hit_val = pd.Series(hits - alpha, index=origins)
    lag_step = pd.Timedelta(hours=h)

    ok = np.ones(len(origins), dtype=bool)
    lag_cols = []
    for k in range(1, DQ_LAGS + 1):
        lagged = hit_val.reindex(origins - k * lag_step)
        ok &= lagged.notna().to_numpy()
        lag_cols.append(lagged.to_numpy())

    d_cols = _dq_indicator_matrix(origins.hour, h)
    X_full = np.column_stack([np.ones(len(origins)), *lag_cols, d_cols])
    Hit_full = hit_val.to_numpy()

    X = X_full[ok]
    Hit = Hit_full[ok]
    return X, Hit


def dq_test(X: np.ndarray, hit: np.ndarray, alpha: float) -> tuple[float, float, int]:
    q = X.shape[1]
    rank = np.linalg.matrix_rank(X)
    if rank < q:
        raise ValueError(f"rank(X)={rank} < q={q}")
    Xty = X.T @ hit
    beta = np.linalg.solve(X.T @ X, Xty)
    dq = float(Xty @ beta) / (alpha * (1 - alpha))
    p = float(chi2.sf(dq, q))
    return dq, p, q


def hac_mean_ci(d: np.ndarray, lag: int = HAC_LAG, z: float = _Z_95) -> tuple[float, float, float]:
    d = np.asarray(d, dtype=float)
    n = len(d)
    dbar = float(d.mean())
    dc = d - dbar
    gamma0 = float(np.sum(dc * dc) / n)
    var = gamma0
    max_lag = min(lag, n - 1)
    for k in range(1, max_lag + 1):
        gk = float(np.sum(dc[k:] * dc[:-k]) / n)
        w = 1 - k / (lag + 1)
        var += 2 * w * gk
    se = np.sqrt(var / n) if var > 0 else 0.0
    return dbar, dbar - z * se, dbar + z * se


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Calibration:
    p_uc: float
    p_cc: float
    p_dq: float
    coverage: float
    n: int


def primary_pass(cal: Calibration, ci_lo_vs_plain_har: float, level: float = P_LEVEL) -> bool:
    return (
        cal.p_uc >= level and cal.p_cc >= level and cal.p_dq >= level
        and ci_lo_vs_plain_har > 0
    )


def overall_verdict(passes: dict[str, bool]) -> str:
    n_pass = sum(1 for v in passes.values() if v)
    if n_pass == len(passes) and n_pass > 0:
        return "PASS"
    if n_pass == 0:
        return "FAIL"
    return "SPLIT"


def gate_4h(primary_passed: bool, cal4: Calibration, ci_lo4: float) -> bool:
    if not primary_passed:
        return False
    return primary_pass(cal4, ci_lo4)


def stage_a_valid(checks: dict[str, bool]) -> tuple[bool, list[str]]:
    failing = [k for k, v in checks.items() if not v]
    return (len(failing) == 0, failing)


# --------------------------------------------------------------------------
# Synthetic seasonal DGP (test/A6 only -- never real data, never scored)
# --------------------------------------------------------------------------

def seasonal_dgp(seed: int, years: float, seasonal: bool = True,
                  start: pd.Timestamp = pd.Timestamp("2020-01-01", tz="UTC"),
                  latent: np.ndarray | None = None) -> pd.DataFrame:
    """A synthetic 15m-bar price path whose hourly RV truly follows the
    candidate's log-HAR-on-deseasonalized-variance model, generated so it
    can be run through the REAL pipeline (hourly_grid -> ... -> walk_forward)
    end to end. Used by the unit tests' synthetic checks and by stage A's
    A6 -- both import this one function rather than restating the DGP.

    - True variance: ln RV*_t = beta . [1, x1, x24, x168](RV*) + N(0, 0.5^2),
      the same HAR recursion the candidate assumes.
    - True seasonality: s = 3.0 at 14 UTC on weekdays, 1 elsewhere (or
      `seasonal=False` for the s=1 control), geometric-mean-normalized.
    - RV_t = s_how(t) * RV*_t; four Gaussian 15m sub-returns per hour with
      variance RV_t/4 each, so their sum of squares is an unbiased (noisy)
      proxy for RV_t -- the same proxy noise the real pipeline has.
    - `latent` (E17's A0 only; None = E15 exactly, same RNG draws): a
      per-hour additive term in ln RV_t applied AFTER the HAR recursion
      (no feedback into it), i.e. a latent log-vol component the HAR
      features only see through past, noisy RV.
    """
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

    warm_mean = _HAR_BETA[0] / max(1e-6, 1 - _HAR_BETA[1] - _HAR_BETA[2] - _HAR_BETA[3])
    ln_rv_star = np.empty(n_hours)
    ln_rv_star[:168] = warm_mean + rng.normal(0.0, 0.5, 168)
    rv_star = np.exp(ln_rv_star[:168])
    rv_star = np.concatenate([rv_star, np.empty(n_hours - 168)])
    for t in range(168, n_hours):
        x1 = ln_rv_star[t - 1]
        x24 = np.log(np.mean(rv_star[t - 24:t]))
        x168 = np.log(np.mean(rv_star[t - 168:t]))
        mean = _HAR_BETA[0] + _HAR_BETA[1] * x1 + _HAR_BETA[2] * x24 + _HAR_BETA[3] * x168
        ln_rv_star[t] = mean + rng.normal(0.0, 0.5)
        rv_star[t] = np.exp(ln_rv_star[t])

    rv_true = rv_star * s
    if latent is not None:
        rv_true = rv_true * np.exp(np.asarray(latent, dtype=float))
    sub_std = np.sqrt(np.maximum(rv_true, RV_FLOOR) / 4.0)
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
