"""E17: Deribit DVOL (30-day implied vol) as ONE extra regressor in E15's
seasonal log-HAR + FHS band. The control is E15's frozen band; the
candidate is the same with `x_iv` appended (`walk_forward(..., extra=)`).

Pure and DB-free. The design is the vault note `Docs/e17-preregistration.md`
(DRAFT until the user freezes it). Every constant below is pinned there.

Also carries the stage-A0 pieces: the gap count, the 9-month development
blocks and their DQ, the kill rules, and `iv_dgp`, the synthetic world for
A0's power/size (never real data). `primary_pass` is the ONE decision rule
-- A0's synthetic power and stage B both call it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from model.range_forecast import (
    ALPHA_PRIMARY,
    dq_design,
    dq_test,
    hac_mean_ci,
    miss,
    seasonal_dgp,
    winkler,
)

DVOL_FIRST = pd.Timestamp("2021-03-24", tz="UTC")
SCORING_CUTOFF = pd.Timestamp("2026-01-01", tz="UTC")
GAP_END = pd.Timestamp("2026-09-27", tz="UTC")
VERDICT_START = pd.Timestamp("2026-01-01", tz="UTC")
VERDICT_END = pd.Timestamp("2026-09-27", tz="UTC")
HOURS_PER_YEAR = 8760

GAP_MAX = 0.05
BLOCK_FAIL_MIN = 5          # of 6, per symbol (>= 80%)
POWER_MIN = 0.50
POWER_KAPPA = 0.5
KAPPAS = (0.0, 0.25, 0.5, 0.75, 1.0)
SD_U_GRID = (0.1, 0.2, 0.3, 0.4, 0.6, 0.8)
HALF_LIFE_H = 24
SIZE_BAND = (0.025, 0.08)
STRONG_POWER_MIN = 0.95
# The "power ~ 1" self-check plant: a large latent component (sd 1.5, 24h)
# that the IV column knows exactly (kappa = 1).
# It need not be realistic -- it proves the harness can see a real effect.
STRONG_SD_U = 1.5
STRONG_HALF_LIFE_H = 24


# --------------------------------------------------------------------------
# Feature and data hygiene
# --------------------------------------------------------------------------

def iv_feature(dvol: pd.DataFrame, origins) -> pd.Series:
    """x_iv at each origin t = ln(close^2 / (1e4 * 8760)) of the hourly DVOL
    candle that STARTS at t - 1h (and so ends at t). Missing -> NaN."""
    origins = pd.DatetimeIndex(origins)
    close = pd.Series(dvol["close"].to_numpy(dtype=float),
                      index=pd.DatetimeIndex(dvol["ts"]))
    close = close[~close.index.duplicated(keep="last")]
    c = close.reindex(origins - pd.Timedelta(hours=1)).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        x = np.log(c ** 2 / (1e4 * HOURS_PER_YEAR))
    return pd.Series(x, index=origins)


def truncate_values(df: pd.DataFrame, col: str = "ts",
                    cutoff: pd.Timestamp = SCORING_CUTOFF) -> pd.DataFrame:
    """Every A0 / stage-A load of VALUES is cut strictly before 2026."""
    return df[pd.DatetimeIndex(df[col]) < cutoff].reset_index(drop=True)


def gap_share(ts, start, end) -> dict:
    """Hourly coverage over [start, end), by timestamp only."""
    expected = pd.date_range(start, end, freq="h", inclusive="left")
    present = pd.DatetimeIndex(ts).unique()
    n_present = int(present.isin(expected).sum())
    missing = len(expected) - n_present
    return {"expected": len(expected), "present": n_present, "missing": missing,
            "share": missing / len(expected) if len(expected) else 1.0}


# --------------------------------------------------------------------------
# The decision rule (ONE definition)
# --------------------------------------------------------------------------

def paired_contrast(wf_control: pd.DataFrame, wf_candidate: pd.DataFrame,
                    alpha: float = ALPHA_PRIMARY):
    """mean(S_control - S_candidate) in bps with E15's HAC 95% CI, on the
    rows both models scored. Returns (mean, lo, hi, n)."""
    common = wf_control.index.intersection(wf_candidate.index)
    a, b = wf_control.loc[common], wf_candidate.loc[common]
    d = (winkler(a["L80"], a["U80"], a["r"], alpha)
         - winkler(b["L80"], b["U80"], b["r"], alpha)) * 1e4
    mean, lo, hi = hac_mean_ci(np.asarray(d))
    return mean, lo, hi, len(common)


def primary_pass(ci_lo: float) -> bool:
    """Stage B's primary per symbol: HAC CI lower bound strictly > 0."""
    return bool(ci_lo > 0)


def two_sided_reject(lo: float, hi: float) -> bool:
    return bool(lo > 0 or hi < 0)


# --------------------------------------------------------------------------
# A0 kill 2: the control's DQ in 9-month development blocks
# --------------------------------------------------------------------------

def dev_blocks() -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    starts = pd.date_range("2021-04-01", periods=7, freq="9MS", tz="UTC")
    return [(starts[i], starts[i + 1]) for i in range(6)]


DEV_STUB = (pd.Timestamp("2025-10-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC"))


def block_dq(wf: pd.DataFrame, blocks, alpha: float = ALPHA_PRIMARY, h: int = 1) -> list[dict]:
    """E15's DQ (q = 9) on the scored hit sequence inside each block."""
    out = []
    for s, e in blocks:
        w = wf[(wf.index >= s) & (wf.index < e)]
        hits = miss(w["r"], w["L80"], w["U80"])
        X, Hit = dq_design(w.index, hits, alpha, h)
        stat, p, _ = dq_test(X, Hit, alpha)
        out.append({"start": str(s.date()), "end": str(e.date()), "n": int(len(w)),
                    "stat": float(stat), "p": float(p), "fail": bool(p < 0.05)})
    return out


def a0_kills(gaps: dict, block_fails: dict, power: float) -> list[str]:
    fired = []
    if any(v > GAP_MAX for v in gaps.values()):
        fired.append("kill1_dvol_gaps")
    if any(v < BLOCK_FAIL_MIN for v in block_fails.values()):
        fired.append("kill2_control_block_fail_rate")
    if power < POWER_MIN:
        fired.append("kill3_power")
    return fired


# --------------------------------------------------------------------------
# A0 kill 3: synthetic power
# --------------------------------------------------------------------------

def choose_sd_u(medians: dict, target: float):
    """Smallest sd_u whose synthetic control median DQ stat reaches target."""
    for sd in sorted(medians):
        if medians[sd] >= target:
            return sd
    return None


def is_monotone(powers, n: int) -> tuple[bool, list[int]]:
    """Non-decreasing up to 2 Monte-Carlo SEs. Returns (ok, dip indices)."""
    dips, ok = [], True
    for i in range(1, len(powers)):
        if powers[i] < powers[i - 1]:
            dips.append(i)
            p = (powers[i] + powers[i - 1]) / 2
            se = np.sqrt(2 * p * (1 - p) / n)
            if powers[i - 1] - powers[i] > 2 * se:
                ok = False
    return ok, dips


def iv_dgp(seed: int, sd_u: float, kappa: float, start: pd.Timestamp,
           years: float, iv_start: pd.Timestamp, half_life: float = HALF_LIFE_H):
    """E15's seasonal HAR world plus a latent AR(1) log-vol component u_t
    (stationary sd = sd_u) that HAR only sees through past RV, and a
    synthetic IV column that knows u up to the LAST hour, never the next
    shock: x_iv,t = sqrt(kappa) * u_{t-1}/sd_u + sqrt(1-kappa) * eta_t.

    Common random numbers: the bars and u depend only on (seed, sd_u); eta
    only on seed -- so kappa changes the IV's information and nothing else.
    Returns (bars_15m, x_iv indexed at hourly origins, u indexed by hour)."""
    n_hours = int(round(years * 365 * 24))
    idx = pd.date_range(start, periods=n_hours, freq="h", tz="UTC")
    rng_u = np.random.default_rng([seed, 1])
    rng_eta = np.random.default_rng([seed, 2])
    phi = 0.5 ** (1.0 / half_life)
    e = rng_u.normal(0.0, 1.0, n_hours)
    u = np.empty(n_hours)
    u[0] = e[0]
    for t in range(1, n_hours):
        u[t] = phi * u[t - 1] + np.sqrt(1 - phi ** 2) * e[t]
    u *= sd_u
    bars = seasonal_dgp(seed=seed, years=years, seasonal=True, start=start, latent=u)
    eta = rng_eta.normal(0.0, 1.0, n_hours)
    u_lag = np.concatenate([[np.nan], u[:-1]])
    x = np.sqrt(kappa) * u_lag / sd_u + np.sqrt(1 - kappa) * eta
    x_iv = pd.Series(x, index=idx).where(idx >= iv_start)
    return bars, x_iv, pd.Series(u, index=idx)


def noise_column(seed: int, index: pd.DatetimeIndex, iv_start: pd.Timestamp) -> pd.Series:
    """An independent pure-noise IV column (the exchangeable null's twin)."""
    eta2 = np.random.default_rng([seed, 3]).normal(0.0, 1.0, len(index))
    return pd.Series(eta2, index=index).where(index >= iv_start)
