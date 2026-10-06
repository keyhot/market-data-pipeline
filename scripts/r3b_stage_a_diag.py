"""R3-b stage A post-run diagnostic (NOT gated, written after S2 failed).

Why did the real-bar placebo reject at 0.087 > 0.08? Re-draws the S2 placebo
on 2024 bars only and records each draw's paired mean and its bootstrap SE, so
a biased placebo (scorer bug: mean != 0) can be told apart from an
under-covering bootstrap (mean ~ 0, spread > bootstrap SE). Reads nothing
outside 2024 and changes no rule. Writes scripts/results/r3b_stage_a_diag.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import wick_catch as wc  # noqa: E402
from scripts import r3b_stage_a as sa  # noqa: E402

DRAWS = 300


def main():
    bars = {s: sa.load(s) for s in sa.SYMS}
    k = json.loads(sa.OUT.read_text())["k"]
    f = {s: wc.wick_fills(bars[s], k=k, tick=wc.TICKS[s], sigma=wc.trailing_sigma_traded(bars[s]),
                          require_hold_present=True) for s in sa.SYMS}
    rng = np.random.default_rng(sa.SEED + 3)
    means, ses, lbs, per_side = [], [], [], {"bid": [], "ask": []}
    for _ in range(DRAWS):
        xs, cs, ds, sides = [], [], [], []
        for s in sa.SYMS:
            pe = wc.placebo_events(bars[s], f[s], rng=rng)
            xs.append(wc.score_fills(bars[s], pe)["net_bps"].to_numpy())
            cs.append(wc.control_returns(bars[s], pe, rng=rng))
            ds.append(pe.index.strftime("%Y-%m-%d"))
            sides.append(pe["side"].to_numpy())
        x, c, d, sd = map(np.concatenate, (xs, cs, ds, sides))
        r = wc.cluster_bootstrap(x, c, d, n_boot=999, rng=rng)
        means.append(r["mean_paired"]); lbs.append(r["lb_paired"])
        ses.append((r["ci95_paired"][1] - r["ci95_paired"][0]) / 3.92)
        for side in per_side:
            per_side[side].append(float(np.mean((x - c)[sd == side])))
    means, ses, lbs = map(np.asarray, (means, ses, lbs))
    out = {"draws": DRAWS, "paired_rejection": float((lbs > 0).mean()),
           "mean_of_paired_means": float(means.mean()), "se_of_that_mean": float(means.std() / np.sqrt(DRAWS)),
           "sd_of_paired_means": float(means.std()), "mean_bootstrap_se": float(ses.mean()),
           "per_side_mean_paired": {k_: float(np.mean(v)) for k_, v in per_side.items()}}
    (ROOT / "scripts" / "results" / "r3b_stage_a_diag.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
