"""E15c continuous vol-target (1x cap) report runner. Pre-registered and
FROZEN 2026-09-30 in the vault, `Docs/e15c-preregistration.md` -- every
window, threshold and formula is fixed there; this script applies them,
never chooses them. A NEW experiment, not an amendment of E15b (i)
(VOID); nothing here uses E15.

Stage A0 is the synthetic power/size check and reads NO real data --
it never calls `load_spot_klines` or touches `--archive-root`. It picks
the pre-declared scoring branch (clean 2017-10..2019-12, or the
contaminated 2020-25 fallback) by power alone, before any real return is
read. Stages A/B/C are guards only in this build (A0-only handoff): they
refuse without the prior stage's result and otherwise raise
NotImplementedError.

    poetry run python scripts/vol_target_report.py --stage A0
    poetry run python scripts/vol_target_report.py --stage A  [--archive-root PATH]
    poetry run python scripts/vol_target_report.py --stage B  [--archive-root PATH]
    poetry run python scripts/vol_target_report.py --stage C  [--archive-root PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from model.spot_archive import load_spot_klines  # noqa: E402
from model.vol_target import (  # noqa: E402
    A0_BOOT_REPS,
    A0_CORR,
    A0_MEAN_LOG_SIGMA,
    A0_PHI_IID,
    A0_PHI_PERSISTENT,
    A0_SD,
    A0_SEEDS,
    A0_WARMUP_MONTHS,
    BOOT_BLOCK,
    CLEAN_MONTHS,
    FALLBACK_MONTHS,
    POWER_GATE,
    SIGMA_STAR,
    SIZE_GATE,
    choose_branch,
    moves_exposure,
    power_sim,
    score_pair,
    target_exposures,
)
from model.vol_managed import (  # noqa: E402
    BOOT_REPS,
    COST_SIDE,
    ES_LEVEL,
    ES_REPORT_DAILY,
    ES_REPORT_MONTHLY,
    LW_LAG,
    L_MAX,
    MIN_SIGMA_DAYS,
    SEED,
    constant_exposure_book,
    daily_returns,
    delta_es,
    expected_shortfall,
    lw_sharpe_diff,
    managed_daily,
    month_sigma,
    paired_block_bootstrap,
    permuted_exposures,
    stage_a_valid,
)
import report_results  # noqa: E402
from report_results import write_result  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")

A0_ALLOWLIST = {"a0_power", "a0_size", "a0_mde", "branch"}

# --------------------------------------------------------------------------
# Stage A -- windows, months, tolerances (pre-reg "Windows" / "Stage A" /
# "Implementation spec")
# --------------------------------------------------------------------------

STAGE_A_ALLOWLIST = {
    "a0_power", "a0_size", "a0_mde", "a1_ci_lo", "a2_ok", "a3_violations",
    "a4_mismatches", "a5_valid_months", "a6_ebar", "a6_months_below", "branch",
}

# A1's window is fixed regardless of which branch A0 chose ("validation-A
# 2020-25, both symbols... stage A only").
VALIDATION_A_FIRST = "2020-01"
VALIDATION_A_LAST = "2025-12"

CLEAN_B_FIRST = "2017-10"
CLEAN_B_LAST = "2019-12"
CONTAM_B_FIRST = "2020-01"
CONTAM_B_LAST = "2025-12"

# A4's fixed months, per branch (pre-reg's A4 row).
A4_MONTHS_CLEAN = ("2017-10", "2018-01", "2018-06", "2018-11", "2019-04", "2019-12")
A4_MONTHS_FALLBACK = (  # E15b's 10 months, reused verbatim (pre-reg: "or E15b's 10 months (fallback)")
    "2020-01", "2020-04", "2021-01", "2021-06", "2022-06",
    "2022-12", "2023-08", "2024-03", "2025-04", "2025-12",
)

# A3's practical tolerance, now declared in the pre-reg (E15b's operational value).
A3_TOL = 1e-9

CUTOFF_2026 = pd.Timestamp("2026-01-01", tz="UTC")
CUTOFF_2020 = pd.Timestamp("2020-01-01", tz="UTC")

STAGE_B_ALLOWLIST = {
    "branch", "per_symbol",
}

# Report-only (pre-reg "Costs"): perp_taker = 0.05% + 1bp = 6bps/side. Never
# gates a verdict. Funding coverage for BTCUSDT/ETHUSDT was confirmed at
# build time (`binance_um/monthly/fundingRate/{BTCUSDT,ETHUSDT}` present
# from 2020-01 on -- matches the contaminated branch exactly), but parsing
# it into the 8h-settlement charge was NOT built here (logged as a declared
# scope cut, report-only, cannot move the verdict): the sensitivity below
# is cost-only, zero funding, on both branches.
PERP_COST_SIDE = 0.0006


# --------------------------------------------------------------------------
# Stage A0 -- synthetic only, reads no real data
# --------------------------------------------------------------------------

def _run_branch_a0(months: int, seeds, reps: int, max_workers: int | None) -> dict:
    power_out = power_sim(months=months, phi=A0_PHI_PERSISTENT, seeds=seeds, reps=reps,
                           max_workers=max_workers)
    size_out = power_sim(months=months, phi=A0_PHI_IID, seeds=seeds, reps=reps,
                          max_workers=max_workers)
    return {
        "power": power_out["pass_rate"],
        "size": size_out["pass_rate"],
        "mde": power_out["mde"],
        "median_se": power_out["median_se"],
        "median_ebar": power_out["median_ebar"],
        "median_months_below_1": power_out["median_months_below_1"],
        "n_paths": power_out["n_paths"],
    }


def run_stage_a0(seeds=None, reps: int = A0_BOOT_REPS, results_dir=None,
                 max_workers: int | None = None):
    """Runs the clean branch's A0 first; only runs the fallback's A0 if
    the clean branch is power-UNDERPOWERED (pre-reg: "Otherwise ... go to
    the fallback's A0"). Reads NO real return, volatility or exposure of
    any real period -- `power_sim` is entirely synthetic."""
    seeds = range(A0_SEEDS) if seeds is None else seeds

    a0: dict = {}
    a0["clean"] = _run_branch_a0(CLEAN_MONTHS, seeds, reps, max_workers)
    branch = "clean" if a0["clean"]["power"] >= POWER_GATE else None
    if branch is None:
        a0["contaminated"] = _run_branch_a0(FALLBACK_MONTHS, seeds, reps, max_workers)
    branch = choose_branch(a0)

    chosen = a0.get(branch) if branch in ("clean", "contaminated") else a0["clean"]
    if branch == "VOID_UNDERPOWERED" and "contaminated" in a0:
        # Report against the fallback's numbers (the branch that was
        # actually checked last / is closer to the gate), per the
        # allowlist's single a0_power/a0_size/a0_mde triple.
        chosen = a0["contaminated"]

    size_gate_ok = bool(chosen["size"] <= SIZE_GATE)
    statistic = {
        "a0_power": chosen["power"],
        "a0_size": chosen["size"],
        "a0_mde": chosen["mde"],
        "branch": branch,
    }
    assert set(statistic.keys()) <= A0_ALLOWLIST, (
        f"stage A0 statistic keys outside the allowlist: {set(statistic) - A0_ALLOWLIST}"
    )

    verdict = branch  # "clean" | "contaminated" | "VOID_UNDERPOWERED"

    path = write_result(
        "e15c_stage_a0",
        inputs={"data": "synthetic only -- no real returns read", "symbols": list(SYMBOLS)},
        parameters={
            "sigma_star": SIGMA_STAR, "l_max": L_MAX, "cost_side": COST_SIDE,
            "boot_block": BOOT_BLOCK, "boot_reps": reps, "boot_seed": SEED,
            "n_seeds": len(list(seeds)) if not isinstance(seeds, range) else len(seeds),
            "phi_persistent": A0_PHI_PERSISTENT, "phi_iid": A0_PHI_IID,
            "sd": A0_SD, "warmup_months": A0_WARMUP_MONTHS,
            "mean_log_sigma": A0_MEAN_LOG_SIGMA, "corr": A0_CORR,
            "clean_months": CLEAN_MONTHS, "fallback_months": FALLBACK_MONTHS,
            "power_gate": POWER_GATE, "size_gate": SIZE_GATE,
        },
        statistic=statistic,
        ci={},
        verdict=verdict,
        extra={
            "clean": a0.get("clean"),
            "contaminated": a0.get("contaminated"),
            "size_gate_ok_on_chosen_branch": size_gate_ok,
            "note": (
                "VOID_UNDERPOWERED means A0-power failed on both branches; "
                "B is never run. A size-gate failure on the chosen branch "
                "(size_gate_ok_on_chosen_branch=False) is a separate VOID "
                "condition the stage-A0 report surfaces but does not fold "
                "into `branch`, since choose_branch()'s three-way return is "
                "power-only per the implementation spec."
            ),
        },
        results_dir=results_dir,
    )
    print(f"stage A0: branch={branch} power={chosen['power']:.3f} "
          f"size={chosen['size']:.3f} mde={chosen['mde']:.4f} -> {path}")
    return branch, a0


# --------------------------------------------------------------------------
# Stage A / B / C -- guards only; scoring bodies are a later worker's task
# --------------------------------------------------------------------------

def load_close(archive_root: Path, symbol: str) -> pd.Series:
    """Daily spot close for stage A, indexed by open date D. No loader
    before stage C may return a bar opening >= 2026-01-01 (pre-reg
    Windows / Implementation spec loader-truncation rule); stage A
    enforces that cutoff itself."""
    raw = load_spot_klines(Path(archive_root) / "binance_spot" / "klines", symbol, "1d")
    raw = raw[raw.index < CUTOFF_2026]
    return raw["close"].astype(float).sort_index()


def load_close_b(archive_root: Path, symbol: str, branch: str) -> pd.Series:
    """Stage-B loader: branch-aware truncation (mutant 7). The clean
    branch never returns a bar opening >= 2020-01-01 (the fallback window
    starts there); the contaminated branch keeps 2020-25 and, like every
    other loader, never returns a bar opening >= 2026-01-01."""
    close = load_close(archive_root, symbol)
    if branch == "clean":
        close = close[close.index < CUTOFF_2020]
    return close


# --------------------------------------------------------------------------
# Stage A -- pure per-check helpers (kept separate from I/O so a validity
# mutant is killable without touching the real archive)
# --------------------------------------------------------------------------

def _a1_oracle(close: pd.Series, r: pd.Series, first, last) -> float:
    """A1: the oracle's exposure vs ITS OWN exposure-matched book (never
    vs BH). Always the validation-A window, regardless of the chosen
    branch."""
    e_oracle = target_exposures(r, first, last, SIGMA_STAR, l_max=L_MAX, oracle=True)
    oracle_book = managed_daily(close, e_oracle, cost_side=COST_SIDE, e_start=1.0)
    ebar_o = float(e_oracle.mean())
    ebar_o_book = constant_exposure_book(
        close, ebar_o, first, last, cost_side=COST_SIDE, e_start=1.0,
    )
    _, lo, _ = paired_block_bootstrap(
        ebar_o_book["y"].to_numpy(), oracle_book["y"].to_numpy(), delta_es,
        block=BOOT_BLOCK, reps=BOOT_REPS, seed=SEED,
    )
    return lo


def _a2_identity(close: pd.Series, r: pd.Series, first, last) -> bool:
    """A2: sigma_star = +inf -> e === l_max (== 1) -> VT equals BH
    bitwise, Delta ES == 0 exactly. Routed through `target_exposures`
    itself (not a hand-built e==1 series) so an L_max default mutant is
    caught here too."""
    e_one = target_exposures(r, first, last, np.inf, l_max=L_MAX, oracle=False)
    vt = managed_daily(close, e_one, cost_side=COST_SIDE, e_start=1.0)
    bh = constant_exposure_book(close, 1.0, first, last, cost_side=COST_SIDE, e_start=1.0)
    same = vt.equals(bh)
    zero_delta = float(delta_es(bh["y"].to_numpy(), vt["y"].to_numpy())) == 0.0
    return bool(same and zero_delta)


def _a3_compounding(close: pd.Series, e: pd.Series, vm: pd.DataFrame, tol: float = A3_TOL) -> int:
    """A3: for every scored month, Pi(1+y_D) == (1-kappa_t)(1+e_t*R_t)."""
    idx = pd.PeriodIndex(pd.DatetimeIndex(vm.index), freq="M")
    close_idx = pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M")
    violations = 0
    for t in e.index:
        rows = vm.loc[idx == t]
        if len(rows) == 0:
            violations += 1
            continue
        prod = float((1.0 + rows["y"]).prod())
        days = close.index[close_idx == t]
        prior_days = close.index[close_idx == (t - 1)]
        if len(days) == 0 or len(prior_days) == 0:
            violations += 1
            continue
        p0 = float(close.loc[prior_days.max()])
        R_t = float(close.loc[days.max()]) / p0 - 1.0
        e_t = float(e.loc[t])
        kappa_t = float(rows["kappa"].iloc[0])
        expected = (1.0 - kappa_t) * (1.0 + e_t * R_t)
        if abs(prod - expected) > tol:
            violations += 1
    return violations


def _a4_truncation(close: pd.Series, months) -> int:
    """A4: exposure at each fixed month, recomputed from data truncated to
    close <= T_{t-1}, must be bitwise equal to the exposure computed from
    the full series."""
    idx = pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M")
    r_full = daily_returns(close)
    mismatches = 0
    for m in months:
        t = pd.Period(m, freq="M")
        e_full = float(target_exposures(r_full, t, t, SIGMA_STAR, l_max=L_MAX, oracle=False).iloc[0])
        cutoff_days = close.index[idx <= (t - 1)]
        close_trunc = close.loc[cutoff_days]
        r_trunc = daily_returns(close_trunc)
        e_trunc = float(target_exposures(r_trunc, t, t, SIGMA_STAR, l_max=L_MAX, oracle=False).iloc[0])
        if e_full != e_trunc:
            mismatches += 1
    return mismatches


def _a5_validity(close: pd.Series, r: pd.Series, first, last) -> int:
    """A5: of the scored months, how many have >=MIN_SIGMA_DAYS sigma
    days (a real sigma_hat) AND every one of their calendar-day bars."""
    sigma_series = month_sigma(r)
    idx = pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M")
    valid = 0
    for t in pd.period_range(first, last, freq="M"):
        sig = sigma_series.get(t, np.nan)
        n_bars = int((idx == t).sum())
        if not (isinstance(sig, float) and np.isnan(sig)) and n_bars == t.days_in_month:
            valid += 1
    return valid


def _a6_moves(r: pd.Series, first, last) -> dict:
    """A6: the lagged rule AND the oracle, on the window being scored --
    both depend on sigma_hat only, never a book return."""
    e_real = target_exposures(r, first, last, SIGMA_STAR, l_max=L_MAX, oracle=False)
    e_oracle = target_exposures(r, first, last, SIGMA_STAR, l_max=L_MAX, oracle=True)
    ebar_r, n_r, ok_r = moves_exposure(e_real)
    ebar_o, n_o, ok_o = moves_exposure(e_oracle)
    return {
        "real": {"ebar": ebar_r, "n_below": n_r, "ok": ok_r},
        "oracle": {"ebar": ebar_o, "n_below": n_o, "ok": ok_o},
    }


def stage_a_checks(a1_lo: float, a2_ok: dict, a3_violations: dict, a4_mismatches: int,
                    a5_valid_months: int, expected_months: int, a6: dict) -> dict[str, bool]:
    return {
        "a1": bool(a1_lo > 0),
        "a2_validation_a": bool(a2_ok["validation_a"]),
        "a2_chosen_b": bool(a2_ok["chosen_b"]),
        "a3_validation_a": a3_violations["validation_a"] == 0,
        "a3_chosen_b": a3_violations["chosen_b"] == 0,
        "a4": a4_mismatches == 0,
        "a5": a5_valid_months == expected_months,
        "a6": bool(a6["real"]["ok"] and a6["oracle"]["ok"]),
    }


def _branch_window(branch: str):
    if branch == "clean":
        return CLEAN_B_FIRST, CLEAN_B_LAST, A4_MONTHS_CLEAN
    return CONTAM_B_FIRST, CONTAM_B_LAST, A4_MONTHS_FALLBACK


def run_stage_a(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a0_path = rd / "e15c_stage_a0.json"
    if not a0_path.exists():
        raise RuntimeError("stage A0 result missing; run --stage A0 first")
    a0_result = json.loads(a0_path.read_text())
    branch = a0_result.get("verdict")
    if branch not in ("clean", "contaminated"):
        raise RuntimeError(f"stage A0 verdict is {branch!r}; refusing")
    if a0_result.get("extra", {}).get("size_gate_ok_on_chosen_branch") is False:
        raise RuntimeError("stage A0 size gate failed on the chosen branch; refusing")

    b_first, b_last, a4_months = _branch_window(branch)
    windows = {
        "validation_a": (VALIDATION_A_FIRST, VALIDATION_A_LAST),
        "chosen_b": (b_first, b_last),
    }
    expected_months = len(pd.period_range(b_first, b_last, freq="M"))

    inputs: dict = {"symbols": list(symbols), "source": "binance spot 1d klines", "branch": branch}
    checks: dict[str, bool] = {}
    a1_by_symbol: dict[str, float] = {}
    a2_by_symbol: dict[str, dict] = {}
    a3_by_symbol: dict[str, dict] = {}
    a4_by_symbol: dict[str, int] = {}
    a5_by_symbol: dict[str, int] = {}
    a6_by_symbol: dict[str, dict] = {}

    for symbol in symbols:
        close = load_close(archive_root, symbol)
        inputs.setdefault("first_bar", {})[symbol] = str(close.index.min())
        inputs.setdefault("last_bar", {})[symbol] = str(close.index.max())
        r = daily_returns(close)

        a1_lo = _a1_oracle(close, r, VALIDATION_A_FIRST, VALIDATION_A_LAST)

        a2_ok: dict = {}
        a3_viol: dict = {}
        for wname, (wf, wl) in windows.items():
            a2_ok[wname] = _a2_identity(close, r, wf, wl)
            e_real_w = target_exposures(r, wf, wl, SIGMA_STAR, l_max=L_MAX, oracle=False)
            vm_real_w = managed_daily(close, e_real_w, cost_side=COST_SIDE, e_start=1.0)
            a3_viol[wname] = _a3_compounding(close, e_real_w, vm_real_w)

        a4_mismatches = _a4_truncation(close, a4_months)
        a5_valid = _a5_validity(close, r, b_first, b_last)
        a6 = _a6_moves(r, b_first, b_last)

        a1_by_symbol[symbol] = a1_lo
        a2_by_symbol[symbol] = a2_ok
        a3_by_symbol[symbol] = a3_viol
        a4_by_symbol[symbol] = a4_mismatches
        a5_by_symbol[symbol] = a5_valid
        a6_by_symbol[symbol] = a6

        sym_checks = stage_a_checks(a1_lo, a2_ok, a3_viol, a4_mismatches, a5_valid,
                                     expected_months, a6)
        for k, v in sym_checks.items():
            checks[f"{k}_{symbol}"] = v

    statistic = {
        "a0_power": a0_result["statistic"]["a0_power"],
        "a0_size": a0_result["statistic"]["a0_size"],
        "a0_mde": a0_result["statistic"]["a0_mde"],
        "a1_ci_lo": a1_by_symbol,
        "a2_ok": a2_by_symbol,
        "a3_violations": a3_by_symbol,
        "a4_mismatches": a4_by_symbol,
        "a5_valid_months": a5_by_symbol,
        "a6_ebar": {s: {"real": v["real"]["ebar"], "oracle": v["oracle"]["ebar"]}
                   for s, v in a6_by_symbol.items()},
        "a6_months_below": {s: {"real": v["real"]["n_below"], "oracle": v["oracle"]["n_below"]}
                            for s, v in a6_by_symbol.items()},
        "branch": branch,
    }
    assert set(statistic.keys()) <= STAGE_A_ALLOWLIST, (
        f"stage A statistic keys outside the allowlist: {set(statistic) - STAGE_A_ALLOWLIST}"
    )

    valid, failing = stage_a_valid(checks)
    verdict = "VALID" if valid else "VOID"

    path = write_result(
        "e15c_stage_a",
        inputs=inputs,
        parameters={
            "sigma_star": SIGMA_STAR, "l_max": L_MAX, "cost_side": COST_SIDE,
            "boot_block": BOOT_BLOCK, "boot_reps": BOOT_REPS, "seed": SEED,
            "min_sigma_days": MIN_SIGMA_DAYS,
            "validation_a": [VALIDATION_A_FIRST, VALIDATION_A_LAST],
            "chosen_b": [b_first, b_last],
            "a4_months": list(a4_months), "a3_tolerance": A3_TOL,
            "expected_months": expected_months,
        },
        statistic=statistic,
        ci={},
        verdict=verdict,
        extra={
            "failing_checks": failing,
            "branch": branch,
        },
        results_dir=results_dir,
    )
    print(f"stage A: {verdict} (failing: {failing}) -> {path}")
    return verdict, failing, statistic


# --------------------------------------------------------------------------
# Stage B report-only stats (private to this runner; never part of the
# gating conjunction -- score_pair's own ci_bh/ci_ebar/pass are, unchanged)
# --------------------------------------------------------------------------

def _max_drawdown(y: pd.Series) -> float:
    r = np.asarray(y, dtype=float)
    if len(r) == 0:
        return 0.0
    curve = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1.0).min())


def _monthly_returns(y: pd.Series) -> np.ndarray:
    idx = pd.PeriodIndex(pd.DatetimeIndex(y.index), freq="M")
    return (1.0 + y).groupby(idx).prod().to_numpy() - 1.0


def _per_year_delta_es(vt_y: pd.Series, ebook_y: pd.Series) -> dict:
    years = sorted(set(vt_y.index.year))
    out = {}
    for yr in years:
        v = vt_y[vt_y.index.year == yr].to_numpy()
        e = ebook_y[ebook_y.index.year == yr].to_numpy()
        if len(v) == 0 or len(e) == 0:
            continue
        out[str(yr)] = float(expected_shortfall(e, ES_LEVEL) - expected_shortfall(v, ES_LEVEL))
    return out


def _report_only_for_symbol(close: pd.Series, books: dict, first, last,
                            cost_side: float, boot_block: int, boot_reps: int,
                            boot_seed: int) -> dict:
    vt, bh, ebook, e = books["vt"], books["bh"], books["ebook"], books["e"]
    vt_y, bh_y, ebook_y = vt["y"], bh["y"], ebook["y"]

    ebar, n_below_095, _ = moves_exposure(e)
    n_below_1 = int((e < 1.0).sum())

    daily_es99 = {
        "vt": expected_shortfall(vt_y.to_numpy(), ES_REPORT_DAILY),
        "bh": expected_shortfall(bh_y.to_numpy(), ES_REPORT_DAILY),
        "ebar_book": expected_shortfall(ebook_y.to_numpy(), ES_REPORT_DAILY),
    }
    monthly_es99 = {
        "vt": expected_shortfall(_monthly_returns(vt_y), ES_REPORT_MONTHLY),
        "bh": expected_shortfall(_monthly_returns(bh_y), ES_REPORT_MONTHLY),
        "ebar_book": expected_shortfall(_monthly_returns(ebook_y), ES_REPORT_MONTHLY),
    }
    max_drawdown = {
        "vt": _max_drawdown(vt_y), "bh": _max_drawdown(bh_y), "ebar_book": _max_drawdown(ebook_y),
    }
    lw_vt_bh = lw_sharpe_diff(vt_y.to_numpy(), bh_y.to_numpy(), lag=LW_LAG)
    lw_vt_ebar = lw_sharpe_diff(vt_y.to_numpy(), ebook_y.to_numpy(), lag=LW_LAG)

    total_cost_paid = float(vt["kappa"].sum())
    turnover = total_cost_paid / cost_side if cost_side > 0 else 0.0

    perm_e = permuted_exposures(e, seed=boot_seed)
    perm_book = managed_daily(close, perm_e, cost_side=cost_side, e_start=1.0)
    perm_y = perm_book["y"]
    pt_pvt, lo_pvt, hi_pvt = paired_block_bootstrap(
        perm_y.to_numpy(), vt_y.to_numpy(), delta_es, block=boot_block, reps=boot_reps, seed=boot_seed,
    )
    pt_pe, lo_pe, hi_pe = paired_block_bootstrap(
        perm_y.to_numpy(), ebook_y.to_numpy(), delta_es, block=boot_block, reps=boot_reps, seed=boot_seed,
    )

    # perps sensitivity (report-only, cost-only, no funding -- see PERP_COST_SIDE docstring)
    perp_vt = managed_daily(close, e, cost_side=PERP_COST_SIDE, e_start=1.0)
    perp_bh = constant_exposure_book(close, 1.0, first, last, cost_side=PERP_COST_SIDE, e_start=1.0)
    perp_ebook = constant_exposure_book(close, ebar, first, last, cost_side=PERP_COST_SIDE, e_start=1.0)
    perp_sensitivity = {
        "cost_side": PERP_COST_SIDE,
        "funding_included": False,
        "note": "funding omitted -- coverage confirmed, parsing deferred; report-only, cannot move the verdict",
        "delta_es_bh_point": delta_es(perp_bh["y"].to_numpy(), perp_vt["y"].to_numpy()),
        "delta_es_ebar_point": delta_es(perp_ebook["y"].to_numpy(), perp_vt["y"].to_numpy()),
    }

    es_ebar_book_975 = expected_shortfall(ebook_y.to_numpy(), ES_LEVEL)

    return {
        "ebar": ebar, "n_months": len(e),
        "months_below_1": n_below_1, "months_below_0_95": n_below_095,
        "daily_es99": daily_es99, "monthly_es99": monthly_es99, "max_drawdown": max_drawdown,
        "lw_sharpe_diff_vt_minus_bh": {"delta": lw_vt_bh[0], "ci_lo": lw_vt_bh[1], "ci_hi": lw_vt_bh[2]},
        "lw_sharpe_diff_vt_minus_ebar": {"delta": lw_vt_ebar[0], "ci_lo": lw_vt_ebar[1], "ci_hi": lw_vt_ebar[2]},
        "total_cost_paid": total_cost_paid, "turnover": turnover,
        "permuted_vs_vt": {"point": pt_pvt, "ci_lo": lo_pvt, "ci_hi": hi_pvt},
        "permuted_vs_ebar_book": {"point": pt_pe, "ci_lo": lo_pe, "ci_hi": hi_pe},
        "permutation_beats_ebar_book": bool(lo_pe > 0),
        "relative_delta_es_ebar": (None if es_ebar_book_975 == 0
                                    else float(books["_ci_ebar_point"] / es_ebar_book_975)),
        "per_year_delta_es_ebar": _per_year_delta_es(vt_y, ebook_y),
        "perp_taker_sensitivity": perp_sensitivity,
    }


def run_stage_b(archive_root: Path, symbols=SYMBOLS, results_dir=None,
                boot_reps: int = BOOT_REPS):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a0_path = rd / "e15c_stage_a0.json"
    a_path = rd / "e15c_stage_a.json"
    if not a0_path.exists():
        raise RuntimeError("stage A0 result missing; run --stage A0 first")
    if not a_path.exists():
        raise RuntimeError("stage A result missing; run --stage A first")
    a0_result = json.loads(a0_path.read_text())
    a_result = json.loads(a_path.read_text())

    branch = a0_result.get("verdict")
    if branch not in ("clean", "contaminated"):
        raise RuntimeError(f"stage A0 verdict is {branch!r}; refusing")
    if a0_result.get("extra", {}).get("size_gate_ok_on_chosen_branch") is False:
        raise RuntimeError("stage A0 size gate failed on the chosen branch; refusing")
    if a_result.get("verdict") != "VALID":
        raise RuntimeError(f"stage A verdict is {a_result.get('verdict')!r}, not VALID; refusing")
    a_branch = a_result.get("extra", {}).get("branch")
    if a_branch != branch:
        raise RuntimeError(
            f"stage A branch {a_branch!r} does not match stage A0 branch {branch!r}; refusing"
        )

    b_first, b_last, _ = _branch_window(branch)
    symbols = tuple(symbols)
    assert len(symbols) == 2, "score_pair scores exactly one pair"
    label_to_symbol = dict(zip(("a", "b"), symbols))

    closes = {s: load_close_b(archive_root, s, branch) for s in symbols}
    close_a, close_b = closes[symbols[0]], closes[symbols[1]]

    result = score_pair(close_a, close_b, b_first, b_last, SIGMA_STAR,
                        cost_side=COST_SIDE, l_max=L_MAX, boot_reps=boot_reps,
                        boot_block=BOOT_BLOCK, boot_seed=SEED, return_books=True)

    n_days = {label_to_symbol[lbl]: len(sym["books"]["vt"]) for lbl, sym in result["symbols"].items()}
    if len(set(n_days.values())) != 1:
        raise RuntimeError(f"symbol day-count mismatch in chosen-B window: {n_days} -- archive gap suspected")

    per_symbol = {}
    report_only = {}
    for lbl, sym in result["symbols"].items():
        symbol = label_to_symbol[lbl]
        pt_bh, lo_bh, hi_bh = sym["ci_bh"]
        pt_e, lo_e, hi_e = sym["ci_ebar"]
        per_symbol[symbol] = {
            "ebar": sym["ebar"], "months_below_1": sym["months_below_1"], "n_months": sym["n_months"],
            "delta_es_bh": {"point": pt_bh, "ci_lo": lo_bh, "ci_hi": hi_bh},
            "delta_es_ebar": {"point": pt_e, "ci_lo": lo_e, "ci_hi": hi_e},
            "pass": sym["pass"],
        }
        books = dict(sym["books"])
        books["_ci_ebar_point"] = pt_e
        report_only[symbol] = _report_only_for_symbol(
            closes[symbol], books, b_first, b_last, COST_SIDE, BOOT_BLOCK, boot_reps, SEED,
        )

    statistic = {"branch": branch, "per_symbol": per_symbol}
    assert set(statistic.keys()) <= STAGE_B_ALLOWLIST, (
        f"stage B statistic keys outside the allowlist: {set(statistic) - STAGE_B_ALLOWLIST}"
    )

    verdict = result["overall"]  # bare "PASS" | "SPLIT" | "FAIL" -- stage C's gate reads this

    path = write_result(
        "e15c_stage_b",
        inputs={"symbols": list(symbols), "source": "binance spot 1d klines", "branch": branch,
               "window": [b_first, b_last]},
        parameters={
            "sigma_star": SIGMA_STAR, "l_max": L_MAX, "cost_side": COST_SIDE,
            "boot_block": BOOT_BLOCK, "boot_reps": boot_reps, "boot_seed": SEED,
            "es_level": ES_LEVEL, "perp_cost_side": PERP_COST_SIDE,
        },
        statistic=statistic,
        ci={},
        verdict=verdict,
        extra={
            "window_label": "(contaminated window)" if branch == "contaminated" else "(clean window)",
            "contamination_caveat": (
                "This branch was run because A0 ruled the clean 2017-10..2019-12 window "
                "UNDERPOWERED; E15c's rule design was shaped by a prior look at 2020-25 "
                "(the E15b A1 audit). A PASS or SPLIT here licenses ONLY the 2026 stage-C "
                "read; it is never quoted alone, on stream or in the plan (pre-reg 'What "
                "this cannot tell us' / Windows table)."
            ) if branch == "contaminated" else None,
            "report_only": report_only,
        },
        results_dir=results_dir,
    )
    print(f"stage B: {verdict} ({branch}) -> {path}")
    return verdict, statistic


def stage_c_symbols(b_result: dict) -> list[str]:
    """Pure gate stub for mutant 8 (C runs without a B pass): given a
    stage-B result dict, returns the symbols allowed into stage C --
    both on PASS, only the passing one on SPLIT -- and refuses anything
    else. Stage C itself is NOT built (out of scope for this handoff)."""
    verdict = b_result.get("verdict")
    if verdict not in ("PASS", "SPLIT"):
        raise RuntimeError(f"stage B verdict is {verdict!r}; refusing")
    per_symbol = b_result["statistic"]["per_symbol"]
    return [sym for sym, v in per_symbol.items() if v.get("pass")]


def run_stage_c(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    b_path = rd / "e15c_stage_b.json"
    if not b_path.exists():
        raise RuntimeError("stage B result missing; run --stage B first")
    b_result = json.loads(b_path.read_text())
    if b_result.get("verdict") not in ("PASS", "SPLIT"):
        raise RuntimeError(f"stage B verdict is {b_result.get('verdict')!r}; refusing")
    raise NotImplementedError("stage C is not built yet -- A0 only, per the handoff")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["A0", "A", "B", "C"], required=True)
    parser.add_argument("--archive-root", default=str(ROOT / "data" / "archive"))
    parser.add_argument("--seeds", type=int, default=None,
                         help="A0 only: override the seed count (default 500, pre-reg-pinned)")
    parser.add_argument("--reps", type=int, default=A0_BOOT_REPS,
                         help="A0 only: bootstrap reps per path (default 2000, pre-reg-pinned)")
    parser.add_argument("--max-workers", type=int, default=None)
    args = parser.parse_args(argv)
    root = Path(args.archive_root)

    if args.stage == "A0":
        seeds = range(args.seeds) if args.seeds is not None else None
        run_stage_a0(seeds=seeds, reps=args.reps, max_workers=args.max_workers)
    elif args.stage == "A":
        run_stage_a(root)
    elif args.stage == "B":
        run_stage_b(root)
    else:
        run_stage_c(root)


if __name__ == "__main__":
    main()
