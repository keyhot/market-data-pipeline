"""R3-b amended stage A (Amendment 1, 2026-10-06): re-check under the bootstrap-t.

Vault ``Docs/r3b-preregistration.md`` §Amendment 1. Same 2024 dev bars, same
k = 8.5 (fills depend on k only, never on the test), every rule-13 (d)/(e)/(f)
check re-run with the studentized day-cluster bootstrap that stage B will use.
Fresh seeds as pre-stated. The stage-A record (r3b_stage_a.json) is not
touched; this writes r3b_stage_a1.json and refuses to overwrite it.

    R3B_CACHE=/path/to/.cache/r3b poetry run python scripts/r3b_stage_a1.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import wick_catch as wc  # noqa: E402
from scripts import r3b_stage_a as sa  # noqa: E402

OUT = ROOT / "scripts" / "results" / "r3b_stage_a1.json"
K = 8.5                                    # stage A rule-5 result, unchanged
SEED_S2 = 20261015
SEED_DEV_BOOT = 20261016
SYN_SEEDS = dict(power30=31, null=32, plant_200=33, sign=34, icc20_clustered=35, icc20_iid=36)
METHOD = "t"


def main():
    if OUT.exists():
        raise SystemExit(f"{OUT} exists: the amended stage A is run once")
    syms = sa.SYMS
    bars = {s: sa.load(s) for s in syms}
    days = wc._eligible_index(*sa.DEV_DAYS).strftime("%Y-%m-%d")
    res = {"git_hash": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                      capture_output=True, text=True).stdout.strip(),
           "amendment": 1, "method": METHOD, "window": list(sa.DEV), "k": K, "symbols": {}}
    gates = {s: wc.data_gate(bars[s], *sa.DEV) for s in syms}
    f = {s: wc.wick_fills(bars[s], k=K, tick=wc.TICKS[s], sigma=wc.trailing_sigma_traded(bars[s]),
                          require_hold_present=True) for s in syms}
    for s in syms:
        res["symbols"][s] = {"gate": gates[s], "fills": int(len(f[s])), "rate_per_day": sa.rate(f[s], days)}

    tup = wc.day_tuples(f[syms[0]].index, f[syms[1]].index, *sa.DEV_DAYS)
    nv = wc.eligible_days(*sa.VERDICT_DAYS)
    P = lambda e, icc, seed, iid=False: wc.power(e, tup, nv, icc=icc, n_seeds=sa.SEEDS, n_boot=sa.BOOT_SYN,
                                                  seed=seed, iid_bootstrap=iid, method=METHOD)
    syn = {"per_day": float(tup[:, :2].sum(1).mean()), "gating_N": float(tup[:, :2].sum(1).mean() * nv),
           "power30": P(30, 0.10, SYN_SEEDS["power30"]), "null": P(0, 0.10, SYN_SEEDS["null"]),
           "plant_200": P(200, 0.10, SYN_SEEDS["plant_200"]), "sign_mutant_-200": P(-200, 0.10, SYN_SEEDS["sign"]),
           "null_icc0.20_clustered": P(0, 0.20, SYN_SEEDS["icc20_clustered"]),
           "null_icc0.20_iid_mutant": P(0, 0.20, SYN_SEEDS["icc20_iid"], iid=True)}
    res["synthetic"] = syn

    def pooled(sc, cc, rng_, n_boot):
        x = np.concatenate([sc[s]["net_bps"].to_numpy() for s in syms])
        c = np.concatenate([cc[s] for s in syms])
        d = np.concatenate([sc[s].index.strftime("%Y-%m-%d") for s in syms])
        ok = np.isfinite(x) & np.isfinite(c)
        return wc.cluster_bootstrap(x[ok], c[ok], d[ok], n_boot=n_boot, rng=rng_, method=METHOD)

    # 2024 point estimates are fixed by stage A; only the bounds change (same control RNG)
    rng = np.random.default_rng(sa.SEED)
    scored = {s: wc.score_fills(bars[s], f[s]) for s in syms}
    ctl = {}
    for s in syms:
        ctl[s] = wc.control_returns(bars[s], f[s], rng=rng)
    res["dev_result"] = pooled(scored, ctl, np.random.default_rng(SEED_DEV_BOOT), sa.BOOT_REAL)
    res["controls_missing"] = {s: int(np.isnan(ctl[s]).sum()) for s in syms}

    # S1 real-bar plant
    pl_sc, pl_ct = {}, {}
    rng_p = np.random.default_rng(sa.SEED)
    for s in syms:
        pb = wc.plant_exit(bars[s], f[s], 200.0)
        pl_sc[s] = wc.score_fills(pb, f[s])
        pl_ct[s] = wc.control_returns(pb, f[s], rng=rng_p)
    pr = pooled(pl_sc, pl_ct, np.random.default_rng(SEED_DEV_BOOT), sa.BOOT_REAL)
    d_prim = pr["mean_primary"] - res["dev_result"]["mean_primary"]
    d_pair = pr["mean_paired"] - res["dev_result"]["mean_paired"]
    res["plant"] = {"delta_primary": d_prim, "delta_paired": d_pair, "pass_test": pr["pass"],
                    "ok": bool(180 <= d_prim <= 225 and 180 <= d_pair <= 225 and pr["pass"])}

    # S2 placebo null, fresh seed, same band
    rej_pair = rej_prim_ctl = 0
    rng_n = np.random.default_rng(SEED_S2)
    for _ in range(sa.PLACEBO_DRAWS):
        psc, pct = {}, {}
        for s in syms:
            pe = wc.placebo_events(bars[s], f[s], rng=rng_n)
            psc[s] = wc.score_fills(bars[s], pe)
            pct[s] = wc.control_returns(bars[s], pe, rng=rng_n)
        r = pooled(psc, pct, rng_n, sa.BOOT_SYN)
        rej_pair += r["lb_paired"] > 0
    res["placebo"] = {"draws": sa.PLACEBO_DRAWS, "seed": SEED_S2, "paired_rejection": rej_pair / sa.PLACEBO_DRAWS}

    checks = {
        "data_gates": all(g["ok"] for g in gates.values()),
        "min_fills": sum(len(v) for v in f.values()) >= sa.MIN_DEV_FILLS,
        "K1_power30_joint>=0.50": syn["power30"]["joint"] >= 0.50,
        "K2_null_primary_in_[0.02,0.08]": 0.02 <= syn["null"]["primary"] <= 0.08,
        "K2_plant200_joint>=0.95": syn["plant_200"]["joint"] >= 0.95,
        "syn_sign_mutant<=0.01": syn["sign_mutant_-200"]["joint"] <= 0.01,
        "syn_iid_mutant_over_rejects": syn["null_icc0.20_iid_mutant"]["primary"]
        > syn["null_icc0.20_clustered"]["primary"],
        "S1_plant": res["plant"]["ok"],
        "S2_placebo_in_[0.02,0.08]": 0.02 <= res["placebo"]["paired_rejection"] <= 0.08,
        "controls_complete": all(v == 0 for v in res["controls_missing"].values()),
    }
    res["stage_a1"] = {"checks": checks, "result": "VALID" if all(checks.values()) else "INVALID"}
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"stage_a1": res["stage_a1"], "placebo": res["placebo"], "dev": res["dev_result"]}, indent=1))


if __name__ == "__main__":
    main()
