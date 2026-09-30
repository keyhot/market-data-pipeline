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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

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
    power_sim,
)
from model.vol_managed import COST_SIDE, L_MAX, SEED  # noqa: E402
import report_results  # noqa: E402
from report_results import write_result  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")

A0_ALLOWLIST = {"a0_power", "a0_size", "a0_mde", "branch"}


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

def run_stage_a(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a0_path = rd / "e15c_stage_a0.json"
    if not a0_path.exists():
        raise RuntimeError("stage A0 result missing; run --stage A0 first")
    a0_result = json.loads(a0_path.read_text())
    if a0_result.get("verdict") not in ("clean", "contaminated"):
        raise RuntimeError(f"stage A0 verdict is {a0_result.get('verdict')!r}; refusing")
    raise NotImplementedError("stage A validity checks are not built yet -- A0 only, per the handoff")


def run_stage_b(archive_root: Path, symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a_path = rd / "e15c_stage_a.json"
    if not a_path.exists():
        raise RuntimeError("stage A result missing; run --stage A first")
    a_result = json.loads(a_path.read_text())
    if a_result.get("verdict") != "VALID":
        raise RuntimeError(f"stage A verdict is {a_result.get('verdict')!r}, not VALID; refusing")
    raise NotImplementedError("stage B scoring is not built yet -- A0 only, per the handoff")


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
