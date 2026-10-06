"""R3-b stage B: the ONE verdict read, 2025-01-01 -> 2026-09-28 (frozen spec + Amendment 1).

Vault ``Docs/r3b-preregistration.md``. Refuses to run unless the amended stage A
(``r3b_stage_a1.json``) is VALID, and refuses to overwrite an existing verdict.
The loader fetches the verdict window only (C1 blocks dropped at parse), slices
to [2025-01-01, 2026-09-28) and asserts it, then the data gate; the scorer is
``model/wick_catch.py`` at the Amendment-1 commit, unchanged. Writes
scripts/results/r3b_stage_b.json.

    OMP_NUM_THREADS=2 poetry run python scripts/r3b_stage_b.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import wick_catch as wc  # noqa: E402

OUT = ROOT / "scripts" / "results" / "r3b_stage_b.json"
STAGE_A1 = ROOT / "scripts" / "results" / "r3b_stage_a1.json"
CACHE = ROOT / ".cache" / "r3b"
SYMS = ("PF_XBTUSD", "PF_ETHUSD")
VERDICT = ("2025-01-01", "2026-09-28")            # [start, end)
VERDICT_DAYS = ("2025-01-01", "2026-09-27")       # eligible days: 614
K = 8.5
BOOT = 9999
METHOD = "t"
SEED_CONTROL = 20261105
SEED_BOOT = 20261106
GATING_N = 1081
MIN_FILLS = 0.5 * GATING_N


def load_verdict(sym, getter=None, cache: Path | None = CACHE) -> pd.DataFrame:
    p = None if cache is None else cache / f"{sym}_verdict.pkl"
    if p is not None and p.exists():
        # Local cache this script wrote itself (gitignored .cache/), never external input.
        b = pd.read_pickle(p)
    else:
        if getter is None:
            from scripts.r3b_a0_report import kraken_getter
            getter = kraken_getter(sym)
        b = wc.fetch_range(getter, pd.Timestamp(VERDICT[0], tz="UTC"), pd.Timestamp(VERDICT[1], tz="UTC"))
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            b.to_pickle(p)
    b = wc.slice_window(b, *VERDICT)
    assert b.index.min() >= pd.Timestamp(VERDICT[0], tz="UTC")
    assert b.index.max() < pd.Timestamp(VERDICT[1], tz="UTC")
    return b


def decide(r: dict, stage_a_valid: bool, gates_ok: bool, n_fills: int) -> str:
    if not (stage_a_valid and gates_ok) or n_fills < MIN_FILLS:
        return "VOID"
    return "PASS" if (r["lb_primary"] > 0 and r["lb_paired"] > 0) else "FAIL"


def _desc(x, c):
    x, c = np.asarray(x, float), np.asarray(c, float)
    return {"n": int(len(x)), "mean_net": float(np.mean(x)) if len(x) else None,
            "mean_control": float(np.mean(c)) if len(x) else None,
            "mean_paired": float(np.mean(x - c)) if len(x) else None}


def main():
    if OUT.exists():
        raise SystemExit(f"{OUT} exists: stage B is run once")
    if not STAGE_A1.exists() or json.loads(STAGE_A1.read_text()).get("stage_a1", {}).get("result") != "VALID":
        raise SystemExit("amended stage A is not VALID: stage B is not run (VOID by rule 11)")
    bars = {s: load_verdict(s) for s in SYMS}
    days = wc._eligible_index(*VERDICT_DAYS).strftime("%Y-%m-%d")
    res = {"git_hash": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                      capture_output=True, text=True).stdout.strip(),
           "window": list(VERDICT), "eligible_days": len(days), "k": K, "method": METHOD,
           "boot": BOOT, "seeds": {"control": SEED_CONTROL, "boot": SEED_BOOT}, "symbols": {}}
    gates = {s: wc.data_gate(bars[s], *VERDICT) for s in SYMS}
    gates_ok = all(g["ok"] for g in gates.values())
    f, scored, ctl = {}, {}, {}
    rng = np.random.default_rng(SEED_CONTROL)
    for s in SYMS:
        f[s] = wc.wick_fills(bars[s], k=K, tick=wc.TICKS[s], sigma=wc.trailing_sigma_traded(bars[s]),
                             require_hold_present=True)
        scored[s] = wc.score_fills(bars[s], f[s])
        ctl[s] = wc.control_returns(bars[s], f[s], rng=rng)
        touch = wc.wick_fills(bars[s], k=K, tick=wc.TICKS[s], touch=True,
                              sigma=wc.trailing_sigma_traded(bars[s]), require_hold_present=True)
        res["symbols"][s] = {
            "n_bars": int(len(bars[s])), "gate": gates[s], "fills": int(len(f[s])),
            "rate_per_day": len(f[s]) / len(days), "bid_share": float((f[s]["side"] == "bid").mean()),
            "one_tick_through_share": float((f[s]["through_ticks"] < 2 - 1e-9).mean()),
            "ambiguous_bars": int(f[s].attrs["ambiguous"]), "touch_fills": int(len(touch)),
            "controls_missing": int(np.isnan(ctl[s]).sum()), "exits_missing": int(scored[s]["exit"].isna().sum())}
    x = np.concatenate([scored[s]["net_bps"].to_numpy() for s in SYMS])
    c = np.concatenate([ctl[s] for s in SYMS])
    d = np.concatenate([scored[s].index.strftime("%Y-%m-%d") for s in SYMS])
    ok = np.isfinite(x) & np.isfinite(c)
    n_fills = int(sum(len(v) for v in f.values()))
    r = wc.cluster_bootstrap(x[ok], c[ok], d[ok], n_boot=BOOT, rng=np.random.default_rng(SEED_BOOT),
                             method=METHOD)
    res["verdict_result"] = r
    res["n_fills"] = n_fills
    res["n_scored"] = int(ok.sum())
    res["verdict"] = decide(r, stage_a_valid=True, gates_ok=gates_ok, n_fills=n_fills)

    # EXPLORATORY, descriptive only (rule 12): cannot rescue a FAIL or downgrade a PASS
    ex = {}
    for s in SYMS:
        sc = scored[s]
        for side in ("bid", "ask"):
            m = (sc["side"] == side).to_numpy()
            ex[f"{s}_{side}"] = _desc(sc["net_bps"].to_numpy()[m], ctl[s][m])
        for yr in (2025, 2026):
            m = (sc.index.year == yr)
            ex[f"{s}_{yr}"] = _desc(sc["net_bps"].to_numpy()[m], ctl[s][m])
        ex[f"{s}_gross_mean"] = float(sc["gross_bps"].mean())
    res["exploratory"] = ex
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"verdict": res["verdict"], "n_fills": n_fills, "result": r,
                      "gates": gates}, indent=1, default=str))


if __name__ == "__main__":
    main()
