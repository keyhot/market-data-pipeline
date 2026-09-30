"""E15c: continuous vol-target exposure (1x cap) on BTC/ETH spot. Pure,
DB-free (Sprint 9 model-plane convention). Pre-registered and FROZEN
2026-09-30 in the vault, `Docs/e15c-preregistration.md`; this module
applies its rule and stage A0 synthetic power/size check, never chooses
them. It is a NEW experiment, not an amendment of E15b (i) (VOID).

The rule, exactly (pre-reg "The rule, exactly"): e_t = min(sigma_star /
sigma_hat_{t-1}, l_max) every month -- no state, no quintile, a fixed
sigma_star. `sigma_hat` reuses `model.vol_managed.month_sigma` /
`_oracle_month_sigma` verbatim (same exclude-last-day / MIN_SIGMA_DAYS
convention); nothing about sigma estimation is redefined here.

Stage A0 (synthetic, no real data) asks whether 27 (clean) or 72
(fallback) months of this rule, under E15b's frozen persistent-regime
DGP (pinned sd=0.4, 1-month warm-up -- NOT E15b's own test-tuned sd=0.6 /
6-month warm-up, which the pre-reg calls out as overstating power), can
tell a vol-timing effect apart from an iid-vol null at all. `power_sim`
runs that check for one regime (phi) at a time; `choose_branch` turns two
branches' power numbers into the pre-declared branch decision.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from model.vol_managed import (
    COST_SIDE,
    L_MAX,
    MIN_SIGMA_DAYS,  # noqa: F401  (re-exported for callers / tests)
    SEED,
    _oracle_month_sigma,
    constant_exposure_book,
    daily_returns,
    delta_es,
    managed_daily,
    month_sigma,
    overall_verdict,
    paired_block_bootstrap,
    symbol_pass,
)

# --------------------------------------------------------------------------
# Pre-registered constants
# --------------------------------------------------------------------------

SIGMA_STAR = 0.50 / np.sqrt(365.0)  # ~0.026171, daily; sigma* = 50%/yr
BOOT_BLOCK = 30
A0_BOOT_REPS = 2_000  # pre-reg: "declared, for compute"; real stage B uses 10,000
A0_SEEDS = 500  # seeds 0..499
A0_SD = 0.4  # pinned stationary sd of ln(sigma); NOT E15b's test value 0.6
A0_WARMUP_MONTHS = 1
A0_MEAN_LOG_SIGMA = float(np.log(0.03))
A0_PHI_PERSISTENT = 0.9
A0_PHI_IID = 0.0
A0_CORR = 0.8  # correlation of the two synthetic symbols' daily shocks
CLEAN_MONTHS = 27
FALLBACK_MONTHS = 72
POWER_GATE = 0.50
SIZE_GATE = 0.10
_Z_ALPHA = 1.959964  # one-sided 2.5% bound
_Z_POWER80 = 0.841621
MDE_Z = _Z_ALPHA + _Z_POWER80  # ~2.8


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

def target_exposures(r: pd.Series, first, last, sigma_star: float,
                      l_max: float = 1.0, oracle: bool = False) -> pd.Series:
    """e_t = min(sigma_star / sigma_hat_ref, l_max) for every month t in
    [first, last] (inclusive), indexed by month (Period[M]).

    Real path (oracle=False): sigma_hat is month t-1's (sigma excludes
    t-1's last day -- `month_sigma`). Oracle path (A1 only): sigma_hat is
    month t's OWN (`_oracle_month_sigma`). No sigma_target, no vol_state --
    unlike E15b's quintile rule, this is a plain constant-target ratio."""
    first = pd.Period(first, freq="M")
    last = pd.Period(last, freq="M")
    months = pd.period_range(first, last, freq="M")
    sigma_hat_series = _oracle_month_sigma(r) if oracle else month_sigma(r)

    out = {}
    for t in months:
        ref_month = t if oracle else (t - 1)
        s_hat = float(sigma_hat_series.loc[ref_month])
        out[t] = float(min(sigma_star / s_hat, l_max))
    return pd.Series(out, dtype=float)


def moves_exposure(e: pd.Series) -> tuple[float, int, bool]:
    """A6, written as a pass condition: ebar <= 0.90 AND at least 1/3 of
    the months have e < 0.95. Both depend only on the exposure series.
    Returns (ebar, n_below_0.95, ok)."""
    ebar = float(e.mean())
    n_below = int((e < 0.95).sum())
    ok = bool(ebar <= 0.90 and n_below * 3 >= len(e))
    return ebar, n_below, ok


# --------------------------------------------------------------------------
# Stage-B pipeline for one pair of symbols (reused, unmodified, by A0's
# per-path scoring so mutants 3/4/5 are cheap to test without 500 seeds)
# --------------------------------------------------------------------------

def score_pair(close_a: pd.Series, close_b: pd.Series, first, last,
               sigma_star: float, cost_side: float = COST_SIDE, l_max: float = L_MAX,
               boot_reps: int = A0_BOOT_REPS, boot_block: int = BOOT_BLOCK,
               boot_seed: int = SEED) -> dict:
    """The pre-reg's stage-B conjunction (Amendment 1, inherited): per
    symbol, PASS iff CI-lo(ES(BH)-ES(VT)) > 0 AND CI-lo(ES(ebar.BH)-ES(VT))
    > 0, via the paired circular block bootstrap. Returns per-symbol ebar,
    both CIs, exposure diagnostics (months with e < 1, for A0's report),
    and the overall PASS/FAIL/SPLIT verdict."""
    per_symbol = {}
    passes = {}
    for label, close in (("a", close_a), ("b", close_b)):
        r = daily_returns(close)
        e = target_exposures(r, first, last, sigma_star, l_max=l_max, oracle=False)
        ebar, _, _ = moves_exposure(e)
        months_below_1 = int((e < 1.0).sum())

        vt = managed_daily(close, e, cost_side=cost_side, e_start=1.0)
        bh = constant_exposure_book(close, 1.0, first, last, cost_side=cost_side, e_start=1.0)
        ebook = constant_exposure_book(close, ebar, first, last, cost_side=cost_side, e_start=1.0)

        pt_bh, lo_bh, hi_bh = paired_block_bootstrap(
            bh["y"].to_numpy(), vt["y"].to_numpy(), delta_es,
            block=boot_block, reps=boot_reps, seed=boot_seed,
        )
        pt_e, lo_e, hi_e = paired_block_bootstrap(
            ebook["y"].to_numpy(), vt["y"].to_numpy(), delta_es,
            block=boot_block, reps=boot_reps, seed=boot_seed,
        )
        ok = symbol_pass(lo_bh, lo_e)
        per_symbol[label] = {
            "ebar": ebar,
            "months_below_1": months_below_1,
            "n_months": len(e),
            "ci_bh": (pt_bh, lo_bh, hi_bh),
            "ci_ebar": (pt_e, lo_e, hi_e),
            "pass": ok,
        }
        passes[label] = ok

    return {"symbols": per_symbol, "overall": overall_verdict(passes)}


# --------------------------------------------------------------------------
# Stage A0: synthetic power/size check
# --------------------------------------------------------------------------

def _regime_pair(seed: int, phi: float, scored_months: int, *,
                  warmup_months: int = A0_WARMUP_MONTHS,
                  mean_log_sigma: float = A0_MEAN_LOG_SIGMA,
                  sd: float = A0_SD, corr: float = A0_CORR):
    """Two synthetic daily closes sharing one AR(1) ln-sigma path (E15b's
    frozen-note DGP, ported here with the pre-reg's PINNED A0 values --
    sd=0.4, 1-month warm-up -- not E15b's test-tuned sd=0.6/6-month
    warm-up). phi=0 collapses the AR(1) recursion to iid ln-sigma (the
    size null); phi=0.9 is the persistent (power) regime. Daily shocks are
    correlated `corr` across the two symbols, same sigma each day.
    Returns (close_a, close_b, first_scored, last_scored, ln_sigma_path)
    -- ln_sigma_path exposed so a test can pin the generator's own
    dispersion independent of any default-value mutation."""
    rng = np.random.default_rng(seed)
    months_n = warmup_months + scored_months
    innov_sd = sd * np.sqrt(1.0 - phi ** 2) if phi != 0.0 else sd
    ln_sig = np.empty(months_n)
    ln_sig[0] = mean_log_sigma
    for i in range(1, months_n):
        ln_sig[i] = mean_log_sigma + phi * (ln_sig[i - 1] - mean_log_sigma) + rng.normal(0.0, innov_sd)
    sigmas = np.exp(ln_sig)

    start = pd.Timestamp("2018-01-01", tz="UTC") - pd.DateOffset(months=warmup_months) - pd.Timedelta(days=1)
    month_starts = pd.period_range(start + pd.Timedelta(days=1), periods=months_n, freq="M")

    rets_a_parts, rets_b_parts = [], []
    for m, sigma in zip(month_starts, sigmas):
        n_days = m.days_in_month
        z1 = rng.normal(0.0, 1.0, n_days)
        eps = rng.normal(0.0, 1.0, n_days)
        z2 = corr * z1 + np.sqrt(1.0 - corr ** 2) * eps
        rets_a_parts.append(sigma * z1)
        rets_b_parts.append(sigma * z2)

    rets_a = np.concatenate(rets_a_parts)
    rets_b = np.concatenate(rets_b_parts)
    idx = pd.date_range(start, periods=len(rets_a) + 1, freq="D", tz="UTC")
    close_a = pd.Series(np.exp(np.cumsum(np.concatenate([[0.0], rets_a]))) * 100.0, index=idx)
    close_b = pd.Series(np.exp(np.cumsum(np.concatenate([[0.0], rets_b]))) * 100.0, index=idx)
    first_scored = month_starts[warmup_months]
    last_scored = month_starts[-1]
    return close_a, close_b, first_scored, last_scored, ln_sig


def _score_one_path(args):
    (seed, phi, months, warmup_months, mean_log_sigma, sd, corr,
     sigma_star, cost_side, l_max, reps, block, boot_seed) = args
    close_a, close_b, first, last, _ln_sig = _regime_pair(
        seed, phi, months, warmup_months=warmup_months,
        mean_log_sigma=mean_log_sigma, sd=sd, corr=corr,
    )
    result = score_pair(close_a, close_b, first, last, sigma_star,
                         cost_side=cost_side, l_max=l_max,
                         boot_reps=reps, boot_block=block, boot_seed=boot_seed)
    return result


def power_sim(months: int, phi: float, seeds, reps: int = A0_BOOT_REPS, *,
              sigma_star: float = SIGMA_STAR, cost_side: float = COST_SIDE,
              l_max: float = L_MAX, sd: float = A0_SD,
              warmup_months: int = A0_WARMUP_MONTHS,
              mean_log_sigma: float = A0_MEAN_LOG_SIGMA, corr: float = A0_CORR,
              block: int = BOOT_BLOCK, boot_seed: int = SEED,
              max_workers: int | None = None) -> dict:
    """Run the full stage-B pipeline over synthetic paths, on ONE regime
    (phi). The caller runs this twice per branch -- phi=0.9 for `power`,
    phi=0.0 for `size` -- since it is agnostic to which label the caller
    attaches to its pass rate.

    Returns: pass_rate (overall PASS share), median_se and mde (of
    Delta ES_ebar, pooled over both symbols and all paths), median_ebar,
    median_months_below_1, n_paths."""
    seed_list = list(seeds) if not isinstance(seeds, int) else list(range(seeds))
    args = [
        (s, phi, months, warmup_months, mean_log_sigma, sd, corr,
         sigma_star, cost_side, l_max, reps, block, boot_seed)
        for s in seed_list
    ]

    if max_workers is None:
        max_workers = min(8, os.cpu_count() or 1)

    results: list[dict]
    if max_workers > 1 and len(seed_list) > 1:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(_score_one_path, args))
    else:
        results = [_score_one_path(a) for a in args]

    n_pass = 0
    se_vals: list[float] = []
    ebar_vals: list[float] = []
    below_vals: list[int] = []
    for res in results:
        if res["overall"] == "PASS":
            n_pass += 1
        for sym in res["symbols"].values():
            _, lo, hi = sym["ci_ebar"]
            se_vals.append((hi - lo) / (2.0 * _Z_ALPHA))
            ebar_vals.append(sym["ebar"])
            below_vals.append(sym["months_below_1"])

    median_se = float(np.median(se_vals))
    return {
        "pass_rate": n_pass / len(results),
        "median_se": median_se,
        "mde": MDE_Z * median_se,
        "median_ebar": float(np.median(ebar_vals)),
        "median_months_below_1": float(np.median(below_vals)),
        "n_paths": len(results),
    }


def choose_branch(a0: dict) -> str:
    """The pre-declared branch decision (Stage A0 gates): clean's power
    first, then the fallback's, else VOID_UNDERPOWERED. Reads no real
    data -- `a0` is entirely synthetic-power numbers."""
    if a0["clean"]["power"] >= POWER_GATE:
        return "clean"
    if a0.get("contaminated", {}).get("power", -1.0) >= POWER_GATE:
        return "contaminated"
    return "VOID_UNDERPOWERED"
