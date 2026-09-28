"""E11: does a cost-clearing 3-class label at h = 4 clear the round trip? (2026-09-28)

Pre-registered in the vault, `Docs/cost-horizon-experiment.md`: the label,
score, centre, per-side gate, arms, control floor and verdict rule are fixed
there, and this script applies them. Expanding walk-forward on 15m bars, test
years 2022..2025; 2026 is never read.

    OMP_NUM_THREADS=3 poetry run python scripts/cost_horizon_report.py --symbol BTCUSDT
"""

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cross_asset_report import _bars  # noqa: E402
from horizon_report import _auc_ci, _fit  # noqa: E402
from live_gate_report import TEST_YEARS  # noqa: E402
from model.cost_horizon import gate_each_side, mechanism, three_class_oos  # noqa: E402
from model.cost_label import _MULTICLASS_PARAMS, DOWN, FLAT, UP  # noqa: E402
from model.features import _normalize, build_features  # noqa: E402
from model.horizon import (  # noqa: E402
    drift_per_bar,
    expanding_oos,
    forward_return,
    positions,
    week_block_ci,
)
from model.train import _NUM_ROUNDS  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
H = 4
COST = 0.0022
COST_BPS = COST * 1e4
CONTROL_MIN_AUC = 0.75
CONTROL_MIN_SKILL_BPS = 44.0
E7_ROWS = 140_250
MIN_ROW_SHARE = 0.99


def _fit_multiclass(X, labels):
    return lgb.train(_MULTICLASS_PARAMS, lgb.Dataset(X, label=labels),
                     num_boost_round=_NUM_ROUNDS)


def _inputs(bars: pd.DataFrame):
    close = _normalize(bars)["close"]
    feats = build_features(bars, horizon_bars=H)
    ret = forward_return(close, H).loc[feats.X.index]
    mu = drift_per_bar(close[close.index.year.isin(TEST_YEARS)])
    return close, feats, ret, mu


def arm_3class(bars, leak: bool = False) -> pd.DataFrame:
    _, feats, ret, _ = _inputs(bars)
    X = feats.X.assign(leak=ret) if leak else feats.X
    return three_class_oos(X, ret, COST, H, TEST_YEARS, _fit_multiclass)


def arm_binary(bars) -> pd.DataFrame:
    _, feats, ret, _ = _inputs(bars)
    oos = expanding_oos(feats.X, feats.y, ret, H, TEST_YEARS, _fit)
    return oos.rename(columns={"p": "score"})


def summarize(label: str, oos: pd.DataFrame, side: pd.Series, close, mu) -> dict:
    pos = positions(side, close, H, mu)
    a, a_lo, a_hi = _auc_ci(oos.rename(columns={"score": "p"}))
    skill, lo, hi = week_block_ci(pos.skill * 1e4, pos.entry.dt.tz_localize(None)
                                  .dt.to_period("W"))
    by_year = pos.groupby("year").skill.mean() * 1e4
    m = mechanism(side, oos.ret, COST)
    print(f"\n{label}: {len(oos):,} predictions, centre "
          f"{oos.groupby('year').centre.first().round(3).to_dict()}")
    print(f"  AUC {a:.4f} [{a_lo:.4f}, {a_hi:.4f}]  calls {m['calls']:,} "
          f"(long share {(side[side != 0] == 1).mean():.3f})")
    print(f"  mechanism: hit {m['hit']:.4f}  miss/hit |ret| "
          f"{m['miss_hit_ratio']:.3f}  cost-clearing hit {m['clearing_hit']:.4f}")
    print(f"  positions {len(pos):,} (long share {(pos.side == 1).mean():.3f}, "
          f"median {pos.bars.median():.0f} bars)")
    print(f"  per position: gross {pos.gross.mean() * 1e4:+.1f}  null "
          f"{pos.null.mean() * 1e4:+.1f}  skill {skill:+.1f} bps "
          f"[{lo:+.1f}, {hi:+.1f}]  vs round trip {COST_BPS:.0f}")
    for sign, name in ((1, "long"), (-1, "short")):
        leg = pos[pos.side == sign]
        print(f"  {name}: {len(leg):,} positions, skill "
              f"{leg.skill.mean() * 1e4:+.1f} bps")
    for year, s in by_year.items():
        print(f"    {year}: skill {s:+.1f} bps  positions "
              f"{(pos.year == year).sum():,}")
    return {"auc": a, "skill": skill, "lo": lo, "rows": len(oos),
            "positive_years": int((by_year > 0).sum()), **m}


def plumbing(oos: pd.DataFrame) -> None:
    print("\nplumbing: test-year class shares (down / flat / up) and argmax firing")
    for year, g in oos.groupby("year"):
        shares = [(g.cls == k).mean() for k in (DOWN, FLAT, UP)]
        fires = [(g.argmax == k).mean() for k in (DOWN, FLAT, UP)]
        print(f"  {year}: classes {' / '.join(f'{s:.3f}' for s in shares)}  "
              f"argmax {' / '.join(f'{f:.3f}' for f in fires)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    bars = _bars(args.symbol)
    close, _, _, mu = _inputs(bars)
    print(f"{args.symbol}: {len(bars):,} 15m bars through 2025, h={H}, "
          f"cost {COST_BPS:.0f} bps")

    ctl_oos = arm_3class(bars, leak=True)
    control = summarize("CONTROL 3class + leaked forward return", ctl_oos,
                        gate_each_side(ctl_oos), close, mu)

    oos = arm_3class(bars)
    oos.to_csv(CACHE_DIR / f"cost_horizon_oos_{args.symbol}_h{H}.csv")
    plumbing(oos)
    primary = summarize("3CLASS (verdict arm)", oos, gate_each_side(oos), close, mu)

    bin_oos = arm_binary(bars)
    binary = summarize("BINARY reference (same gate)", bin_oos,
                       gate_each_side(bin_oos), close, mu)

    argmax_side = pd.Series(np.select([oos.argmax == UP, oos.argmax == DOWN],
                                      [1, -1], 0), index=oos.index)
    if (argmax_side != 0).any():
        summarize("ARGMAX (exploratory, no gate)", oos, argmax_side, close, mu)
    else:
        print("\nARGMAX (exploratory): never fires off `flat`")

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
    print(f"\n{args.symbol} E11 verdict: {verdict} (skill {primary['skill']:+.1f} "
          f"≥ {COST_BPS:.0f}: {primary['skill'] >= COST_BPS}; CI above 0: "
          f"{primary['lo'] > 0}; positive years {primary['positive_years']}/4)")
    print(f"mechanism: miss/hit 3class {primary['miss_hit_ratio']:.3f} vs binary "
          f"{binary['miss_hit_ratio']:.3f}; cost-clearing hit "
          f"{primary['clearing_hit']:.4f} vs {binary['clearing_hit']:.4f}")


if __name__ == "__main__":
    main()
