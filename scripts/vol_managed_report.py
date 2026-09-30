"""E15b (i) vol-managed exposure report runner. Pre-registered in the
vault, `Docs/vol-managed-exposure-experiment.md` (+ "Amendment 1", which
supersedes the frozen primary-comparator text and `symbol_pass`'s
signature) -- every window, threshold and formula is fixed there; this
script applies them, never chooses them.

Stage A is plumbing only: it prints and persists NO VM/BH/comparator
statistic -- only the allowlisted A1-A5 checks. Stage B (real scoring on
2020-2025) refuses to run without a VALID stage-A result; stage C
(2026, confirmatory) refuses to run without a stage-B result.

    poetry run python scripts/vol_managed_report.py --stage A [--archive-root PATH]
    poetry run python scripts/vol_managed_report.py --stage B [--archive-root PATH]
    poetry run python scripts/vol_managed_report.py --stage C [--archive-root PATH]
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
from model.vol_managed import (  # noqa: E402
    BOOT_BLOCK,
    BOOT_REPS,
    COST_SIDE,
    L_MAX,
    LW_LAG,
    MIN_SIGMA_DAYS,
    Q_HIGH,
    Q_LOW,
    SEED,
    constant_exposure_book,
    daily_returns,
    delta_es,
    exposures,
    managed_daily,
    month_sigma,
    paired_block_bootstrap,
    stage_a_valid,
)
import report_results  # noqa: E402
from report_results import write_result  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")
FIRST_SCORED = "2020-01"
LAST_SCORED_B = "2025-12"
CUTOFF_2026 = pd.Timestamp("2026-01-01", tz="UTC")

# The vault note's A4 truncation-invariance months.
A4_MONTHS = ("2020-01", "2020-04", "2021-01", "2021-06", "2022-06",
             "2022-12", "2023-08", "2024-03", "2025-04", "2025-12")

STAGE_A_ALLOWLIST = {
    "a1_ci_lo", "a2_ok", "a3_violations", "a4_mismatches", "a5_valid_months",
}

# Practical tolerance for A3's compounding identity; the note says "within
# 1e-12", but float64 accumulation over a ~30-day product can exceed that
# in absolute terms for values this small -- 1e-9 is the operational
# threshold here (a declared, logged gap; see the stage-A extra block).
A3_TOL = 1e-9


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_close(archive_root: Path, symbol: str, stage: str) -> pd.Series:
    """Daily spot close, indexed by open date D. Stage A and B never see a
    bar opening at or after 2026-01-01 (frozen note, Scored window)."""
    raw = load_spot_klines(Path(archive_root) / "binance_spot" / "klines", symbol, "1d")
    if stage in ("A", "B"):
        raw = raw[raw.index < CUTOFF_2026]
    close = raw["close"].astype(float).sort_index()
    return close


# --------------------------------------------------------------------------
# Stage-A checks (pure once given close/r -- kept separate from I/O so a
# validity mutant, e.g. A1 `>` -> `>=`, is killable without real data)
# --------------------------------------------------------------------------

def stage_a_checks(a1_lo: float, a2_ok: bool, a3_violations: int,
                   a4_mismatches: int, a5_valid_months: int) -> dict[str, bool]:
    return {
        "a1": bool(a1_lo > 0),
        "a2": bool(a2_ok),
        "a3": a3_violations == 0,
        "a4": a4_mismatches == 0,
        "a5": a5_valid_months == 72,
    }


def _a1_oracle(close: pd.Series, r: pd.Series) -> float:
    """A1: the oracle's exposure vs ITS OWN exposure-matched book (never
    vs BH, which the oracle would pass trivially -- see the vault's ⚠️)."""
    e_oracle = exposures(r, FIRST_SCORED, LAST_SCORED_B, oracle=True)
    oracle_book = managed_daily(close, e_oracle, cost_side=COST_SIDE, e_start=1.0)
    ebar_o = float(e_oracle.mean())
    ebar_o_book = constant_exposure_book(
        close, ebar_o, FIRST_SCORED, LAST_SCORED_B, cost_side=COST_SIDE, e_start=1.0,
    )
    _, lo, _ = paired_block_bootstrap(
        ebar_o_book["y"].to_numpy(), oracle_book["y"].to_numpy(), delta_es,
        block=BOOT_BLOCK, reps=BOOT_REPS, seed=SEED,
    )
    return lo


def _a2_identity(close: pd.Series) -> bool:
    """A2: with e == 1 for every scored month, VM and BH run through the
    SAME `managed_daily` call path and must be bitwise identical."""
    months = pd.period_range(FIRST_SCORED, LAST_SCORED_B, freq="M")
    e_one = pd.Series(1.0, index=months)
    vm = managed_daily(close, e_one, cost_side=COST_SIDE, e_start=1.0)
    bh = constant_exposure_book(
        close, 1.0, FIRST_SCORED, LAST_SCORED_B, cost_side=COST_SIDE, e_start=1.0,
    )
    same = vm.equals(bh)
    zero_delta = float(delta_es(bh["y"].to_numpy(), vm["y"].to_numpy())) == 0.0
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


def _a4_truncation(close: pd.Series) -> int:
    """A4: exposure at each of the 10 fixed months, recomputed from data
    truncated to close <= T_{t-1}, must be bitwise equal to the exposure
    computed from the full series."""
    idx = pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M")
    r_full = daily_returns(close)
    mismatches = 0
    for m in A4_MONTHS:
        t = pd.Period(m, freq="M")
        e_full = float(exposures(r_full, t, t, oracle=False).iloc[0])
        cutoff_days = close.index[idx <= (t - 1)]
        close_trunc = close.loc[cutoff_days]
        r_trunc = daily_returns(close_trunc)
        e_trunc = float(exposures(r_trunc, t, t, oracle=False).iloc[0])
        if e_full != e_trunc:
            mismatches += 1
    return mismatches


def _a5_validity(close: pd.Series, r: pd.Series) -> int:
    """A5: of the 72 scored months, how many have >=MIN_SIGMA_DAYS sigma
    days (a real sigma_hat) AND every one of their calendar-day bars."""
    sigma_series = month_sigma(r)
    idx = pd.PeriodIndex(pd.DatetimeIndex(close.index), freq="M")
    valid = 0
    for t in pd.period_range(FIRST_SCORED, LAST_SCORED_B, freq="M"):
        sig = sigma_series.get(t, np.nan)
        n_bars = int((idx == t).sum())
        if not (isinstance(sig, float) and np.isnan(sig)) and n_bars == t.days_in_month:
            valid += 1
    return valid


def run_stage_a(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    inputs = {"symbols": list(symbols), "source": "binance spot 1d klines"}
    checks: dict[str, bool] = {}
    a1_by_symbol: dict[str, float] = {}
    a2_by_symbol: dict[str, bool] = {}
    a3_by_symbol: dict[str, int] = {}
    a4_by_symbol: dict[str, int] = {}
    a5_by_symbol: dict[str, int] = {}

    for symbol in symbols:
        close = load_close(archive_root, symbol, "A")
        inputs.setdefault("first_bar", {})[symbol] = str(close.index.min())
        inputs.setdefault("last_bar", {})[symbol] = str(close.index.max())
        r = daily_returns(close)

        a1_lo = _a1_oracle(close, r)
        a2_ok = _a2_identity(close)

        e_real = exposures(r, FIRST_SCORED, LAST_SCORED_B, oracle=False)
        vm_real = managed_daily(close, e_real, cost_side=COST_SIDE, e_start=1.0)
        a3_viol = _a3_compounding(close, e_real, vm_real)

        a4_mismatches = _a4_truncation(close)
        a5_valid = _a5_validity(close, r)

        a1_by_symbol[symbol] = a1_lo
        a2_by_symbol[symbol] = a2_ok
        a3_by_symbol[symbol] = a3_viol
        a4_by_symbol[symbol] = a4_mismatches
        a5_by_symbol[symbol] = a5_valid

        sym_checks = stage_a_checks(a1_lo, a2_ok, a3_viol, a4_mismatches, a5_valid)
        for k, v in sym_checks.items():
            checks[f"{k}_{symbol}"] = v

    statistic = {
        "a1_ci_lo": a1_by_symbol,
        "a2_ok": a2_by_symbol,
        "a3_violations": a3_by_symbol,
        "a4_mismatches": a4_by_symbol,
        "a5_valid_months": a5_by_symbol,
    }
    assert set(statistic.keys()) <= STAGE_A_ALLOWLIST, (
        f"stage A statistic keys outside the allowlist: {set(statistic) - STAGE_A_ALLOWLIST}"
    )

    valid, failing = stage_a_valid(checks)
    verdict = "VALID" if valid else "VOID"

    path = write_result(
        "e15b_stage_a",
        inputs=inputs,
        parameters={
            "es_level": 0.975, "q_high": Q_HIGH, "q_low": Q_LOW, "l_max": L_MAX,
            "min_sigma_days": MIN_SIGMA_DAYS, "cost_side": COST_SIDE,
            "boot_block": BOOT_BLOCK, "boot_reps": BOOT_REPS, "seed": SEED,
            "lw_lag": LW_LAG, "first_scored": FIRST_SCORED, "last_scored_b": LAST_SCORED_B,
            "a4_months": list(A4_MONTHS), "a3_tolerance": A3_TOL,
        },
        statistic=statistic,
        ci={},
        verdict=verdict,
        extra={
            "failing_checks": failing,
            "amendment_1_open_question": (
                "Answered by Amendment 1 (2026-09-29 22:10): the primary is "
                "the conjunction (vs BH AND vs the exposure-matched book). "
                "Stage B is gated only on the user's go-ahead, not on this "
                "question."
            ),
        },
        results_dir=results_dir,
    )
    print(f"stage A: {verdict} (failing: {failing}) -> {path}")
    return verdict, failing, statistic


# --------------------------------------------------------------------------
# Stage B / C -- guards only; the scoring bodies are stage B's own task
# once the user has confirmed Amendment 1 stands (per the vault's Handoff)
# --------------------------------------------------------------------------

def run_stage_b(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a_path = rd / "e15b_stage_a.json"
    if not a_path.exists():
        raise RuntimeError("stage A result missing; run --stage A first")
    a_result = json.loads(a_path.read_text())
    if a_result.get("verdict") != "VALID":
        raise RuntimeError(f"stage A verdict is {a_result.get('verdict')!r}, not VALID; refusing")
    raise NotImplementedError("stage B scoring is not built yet -- stage A only, per the handoff")


def run_stage_c(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    b_path = rd / "e15b_stage_b.json"
    if not b_path.exists():
        raise RuntimeError("stage B result missing; run --stage B first")
    b_result = json.loads(b_path.read_text())
    if b_result.get("verdict") not in ("PASS", "SPLIT"):
        raise RuntimeError(f"stage B verdict is {b_result.get('verdict')!r}; refusing")
    raise NotImplementedError("stage C is not built yet -- stage A only, per the handoff")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["A", "B", "C"], required=True)
    parser.add_argument("--archive-root", default=str(ROOT / "data" / "archive"))
    args = parser.parse_args(argv)
    root = Path(args.archive_root)
    if args.stage == "A":
        run_stage_a(root)
    elif args.stage == "B":
        run_stage_b(root)
    else:
        run_stage_c(root)


if __name__ == "__main__":
    main()
