"""Is the live model's confidence worth gating on? (2026-09-27)

The on-air model (15m bars, one bar ahead) publishes a call only when
|p - 0.5| clears a cutoff taken from its own holdout. This measures whether
that selection means anything: an expanding walk-forward — train on every bar
before year Y, purge one bar, test on year Y — over 2022..2025, scoring each
year's predictions by confidence quintile. 2026 is the untouched holdout
(model plan E6) and is never read here.

    poetry run python scripts/live_gate_report.py --symbol BTCUSDT
"""

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.features import build_features
from model.train import _NUM_ROUNDS, _PARAMS
from storage.postgres_store import get_price_bars

TEST_YEARS = (2022, 2023, 2024, 2025)
HORIZON = 1


def auc(y: np.ndarray, p: np.ndarray) -> float:
    ranks = pd.Series(p).rank().to_numpy()
    pos = y == 1
    n_pos, n_neg = pos.sum(), (~pos).sum()
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def collect(bars: pd.DataFrame) -> pd.DataFrame:
    feats = build_features(bars, horizon_bars=HORIZON)
    X, y = feats.X, feats.y
    years = X.index.year
    chunks = []
    for year in TEST_YEARS:
        test = np.flatnonzero(years == year)
        train_end = test[0] - HORIZON  # purge: the last label peeks one bar
        booster = lgb.train(
            _PARAMS, lgb.Dataset(X.iloc[:train_end], label=y.iloc[:train_end]),
            num_boost_round=_NUM_ROUNDS,
        )
        chunks.append(pd.DataFrame({
            "year": year, "p": booster.predict(X.iloc[test]),
            "y": y.iloc[test].to_numpy(),
        }, index=X.index[test]))
        print(f"  {year}: trained on {train_end:,} rows, tested {len(test):,}",
              flush=True)
    return pd.concat(chunks)


def report(oos: pd.DataFrame) -> None:
    oos = oos.assign(conf=(oos.p - 0.5).abs(),
                     hit=((oos.p >= 0.5).astype(int) == oos.y).astype(int))
    print(f"\npooled AUC {auc(oos.y.to_numpy(), oos.p.to_numpy()):.4f} over "
          f"{len(oos):,} predictions; base rate up {oos.y.mean():.3f}")
    for year, g in oos.groupby("year"):
        print(f"  {year}: AUC {auc(g.y.to_numpy(), g.p.to_numpy()):.4f}  "
              f"hit {g.hit.mean():.4f}")
    # Quintiles within each year: the p-scale drifts between refits, and the
    # live gate is a quantile of its own model's holdout, not a global number.
    oos["q"] = oos.groupby("year").conf.transform(
        lambda c: pd.qcut(c, 5, labels=False, duplicates="drop"))
    print("\nhit rate by confidence quintile (0 = least confident), ±1.96 se")
    for q, g in oos.groupby("q"):
        n, h = len(g), g.hit.mean()
        se = np.sqrt(h * (1 - h) / n)
        print(f"  q{int(q)}: n={n:,}  |p-0.5| {g.conf.min():.3f}..{g.conf.max():.3f}"
              f"  hit {h:.4f} ± {1.96 * se:.4f}")
    top = oos[oos.q == 4]
    rest = oos[oos.q < 4]
    diff = top.hit.mean() - rest.hit.mean()
    se = np.sqrt(top.hit.var() / len(top) + rest.hit.var() / len(rest))
    print(f"\ntop quintile minus the rest: {diff:+.4f} (z {diff / se:.2f})")
    for year, g in oos.groupby("year"):
        print(f"  {year}: top {g[g.q == 4].hit.mean():.4f}  "
              f"rest {g[g.q < 4].hit.mean():.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    bars = pd.DataFrame(get_price_bars(args.symbol, "15m", 10_000_000))
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars[stamps < "2026-01-01"]  # E6: 2026 is never read here
    print(f"{args.symbol}: {len(bars):,} 15m bars through 2025")
    report(collect(bars))


if __name__ == "__main__":
    main()
