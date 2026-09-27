"""E10: does v0 clear the round trip when the target is 4h direction? (2026-09-27)

Pre-registered in the vault, `Docs/horizon-experiment.md`: the horizons, the
centring, the positions, the drift null, the control and the verdict rule are
fixed there, and this script applies them. Only h = 16 carries a verdict;
the other horizons are exploratory. Expanding walk-forward on 15m bars, test
years 2022..2025; 2026 is never read.

    poetry run python scripts/horizon_report.py --symbol BTCUSDT
"""

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cross_asset_report import _bars  # noqa: E402
from live_gate_report import TEST_YEARS, auc  # noqa: E402
from model.features import _normalize, build_features  # noqa: E402
from model.horizon import (  # noqa: E402
    BOOTSTRAP,
    SEED,
    drift_per_bar,
    expanding_oos,
    forward_return,
    gate_top_fifth,
    per_call_proxy_bps,
    positions,
    week_block_ci,
)
from model.train import _NUM_ROUNDS, _PARAMS  # noqa: E402

PRIMARY = 16
EXPLORATORY = (4, 32, 96)
COST_BPS = 22.0
CONTROL_MIN_AUC = 0.75
CONTROL_MIN_SKILL_BPS = 44.0
E7_ROWS = 140_250
MIN_ROW_SHARE = 0.99


def _fit(X, y):
    return lgb.train(_PARAMS, lgb.Dataset(X, label=y), num_boost_round=_NUM_ROUNDS)


def _auc_ci(oos: pd.DataFrame) -> tuple[float, float, float]:
    y, p = oos.y.to_numpy(), oos.p.to_numpy()
    codes, _ = pd.factorize(oos.index.tz_localize(None).to_period("W"))
    rows_of = [np.flatnonzero(codes == k) for k in range(codes.max() + 1)]
    rng = np.random.default_rng(SEED)
    draws = []
    for _ in range(BOOTSTRAP):
        pick = np.concatenate([rows_of[i] for i in
                               rng.integers(0, len(rows_of), len(rows_of))])
        draws.append(auc(y[pick], p[pick]))
    low, high = np.percentile(draws, [2.5, 97.5])
    return auc(y, p), float(low), float(high)


def run(bars: pd.DataFrame, h: int, leak: bool = False) -> dict:
    """One arm at one horizon. `leak` adds the h-bar forward return as a
    feature: the positive control, which must light up every stage."""
    close = _normalize(bars)["close"]
    feats = build_features(bars, horizon_bars=h)
    ret = forward_return(close, h).loc[feats.X.index]
    X = feats.X.assign(leak=ret) if leak else feats.X
    oos = expanding_oos(X, feats.y, ret, h, TEST_YEARS, _fit)
    gated = gate_top_fifth(oos)
    side = (np.sign(oos.p - oos.centre) * gated).astype(int)
    mu = drift_per_bar(close[close.index.year.isin(TEST_YEARS)])
    pos = positions(side, close, h, mu)
    top = oos[gated]
    return {"oos": oos, "pos": pos, "gated": gated,
            "hit": float(((top.p > top.centre).astype(int) == top.y).mean())}


def summarize(label: str, r: dict) -> dict:
    oos, pos = r["oos"], r["pos"]
    a, a_lo, a_hi = _auc_ci(oos)
    skill, lo, hi = week_block_ci(pos.skill * 1e4, pos.entry.dt.tz_localize(None)
                                  .dt.to_period("W"))
    by_year = pos.groupby("year").skill.mean() * 1e4
    print(f"\n{label}: {len(oos):,} predictions, centre "
          f"{oos.groupby('year').centre.first().round(3).to_dict()}")
    print(f"  AUC {a:.4f} [{a_lo:.4f}, {a_hi:.4f}]  top-5th hit {r['hit']:.4f}")
    print(f"  positions {len(pos):,} (long share {(pos.side == 1).mean():.3f}, "
          f"median {pos.bars.median():.0f} bars)")
    print(f"  per position: gross {pos.gross.mean() * 1e4:+.1f}  null "
          f"{pos.null.mean() * 1e4:+.1f}  skill {skill:+.1f} bps "
          f"[{lo:+.1f}, {hi:+.1f}]  vs round trip {COST_BPS:.0f}")
    print(f"  per-call proxy (drift removed) "
          f"{per_call_proxy_bps(oos, r['gated']):+.2f} bps")
    for year, s in by_year.items():
        g = oos[oos.year == year]
        print(f"    {year}: skill {s:+.1f} bps  AUC "
              f"{auc(g.y.to_numpy(), g.p.to_numpy()):.4f}  positions "
              f"{(pos.year == year).sum():,}")
    return {"auc": a, "skill": skill, "lo": lo, "rows": len(oos),
            "positive_years": int((by_year > 0).sum())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--primary-only", action="store_true")
    args = parser.parse_args()
    bars = _bars(args.symbol)
    print(f"{args.symbol}: {len(bars):,} 15m bars through 2025")

    control = summarize(f"CONTROL h={PRIMARY} (leaked forward return)",
                        run(bars, PRIMARY, leak=True))
    primary = summarize(f"PRIMARY h={PRIMARY}", run(bars, PRIMARY))

    void = []
    if control["auc"] < CONTROL_MIN_AUC or control["skill"] < CONTROL_MIN_SKILL_BPS:
        void.append("control did not light up")
    if primary["rows"] < MIN_ROW_SHARE * E7_ROWS:
        void.append(f"rows {primary['rows']:,} < 99% of {E7_ROWS:,}")
    if void:
        verdict = "VOID (" + "; ".join(void) + ")"
    elif (primary["skill"] >= COST_BPS and primary["lo"] > 0
          and primary["positive_years"] >= 3):
        verdict = "PASS"
    else:
        verdict = "FAIL"
    print(f"\n{args.symbol} h={PRIMARY} verdict: {verdict} (skill "
          f"{primary['skill']:+.1f} ≥ {COST_BPS:.0f}: {primary['skill'] >= COST_BPS}; "
          f"CI above 0: {primary['lo'] > 0}; positive years "
          f"{primary['positive_years']}/4)")

    if not args.primary_only:
        print("\n--- exploratory (no verdict) ---")
        for h in EXPLORATORY:
            summarize(f"h={h}", run(bars, h))


if __name__ == "__main__":
    main()
