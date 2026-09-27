"""E7: do the other symbol's features add ranking skill? (2026-09-27)

Pre-registered in the vault, `Docs/cross-asset-experiment.md` — config and
verdict rule are fixed there, and this script applies them rather than
choosing them. Expanding walk-forward on 15m bars, h = 1, test years
2022..2025; 2026 is never read.

    poetry run python scripts/cross_asset_report.py --symbol ETHUSDT
"""

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_gate_report import HORIZON, TEST_YEARS, auc  # noqa: E402
from model.cross_asset import with_cross_features  # noqa: E402
from model.features import build_features  # noqa: E402
from model.train import _NUM_ROUNDS, _PARAMS  # noqa: E402
from storage.postgres_store import get_price_bars  # noqa: E402

OTHER = {"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT"}
BOOTSTRAP = 1_000
SEED = 7


def _bars(symbol: str) -> pd.DataFrame:
    bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    return bars[stamps < "2026-01-01"]  # E6: 2026 is never read


def collect(symbol: str) -> pd.DataFrame:
    own = build_features(_bars(symbol), horizon_bars=HORIZON)
    X_cross, y = with_cross_features(own.X, own.y, _bars(OTHER[symbol]))
    X_base = X_cross[list(own.X.columns)]  # same rows in both arms
    years = X_cross.index.year
    chunks = []
    for year in TEST_YEARS:
        test = np.flatnonzero(years == year)
        train_end = test[0] - HORIZON
        row = {"year": year, "y": y.iloc[test].to_numpy()}
        for arm, X in (("base", X_base), ("cross", X_cross)):
            booster = lgb.train(
                _PARAMS, lgb.Dataset(X.iloc[:train_end], label=y.iloc[:train_end]),
                num_boost_round=_NUM_ROUNDS,
            )
            row[arm] = booster.predict(X.iloc[test])
        chunks.append(pd.DataFrame(row, index=X_cross.index[test]))
        print(f"  {year}: {train_end:,} train rows, {len(test):,} test", flush=True)
    return pd.concat(chunks)


def delta_auc_ci(oos: pd.DataFrame) -> tuple[float, float, float]:
    """Pooled ΔAUC and a paired day-block bootstrap 95% interval."""
    y, base, cross = oos.y.to_numpy(), oos.base.to_numpy(), oos.cross.to_numpy()
    point = auc(y, cross) - auc(y, base)
    day = oos.index.floor("D")
    codes, _ = pd.factorize(day)
    rows_of = pd.Series(np.arange(len(oos))).groupby(codes).apply(np.array).to_list()
    rng = np.random.default_rng(SEED)
    draws = []
    for _ in range(BOOTSTRAP):
        pick = np.concatenate([rows_of[i] for i in
                               rng.integers(0, len(rows_of), len(rows_of))])
        draws.append(auc(y[pick], cross[pick]) - auc(y[pick], base[pick]))
    low, high = np.percentile(draws, [2.5, 97.5])
    return point, float(low), float(high)


def top_quintile_hit(g: pd.DataFrame, arm: str) -> float:
    conf = (g[arm] - 0.5).abs()
    top = g[conf >= conf.quantile(0.8)]
    return float(((top[arm] >= 0.5).astype(int) == top.y).mean())


def report(symbol: str, oos: pd.DataFrame) -> str:
    point, low, high = delta_auc_ci(oos)
    print(f"\n{symbol} (+ {OTHER[symbol]} features), {len(oos):,} predictions")
    print(f"  pooled AUC base {auc(oos.y.to_numpy(), oos.base.to_numpy()):.4f}"
          f"  cross {auc(oos.y.to_numpy(), oos.cross.to_numpy()):.4f}")
    print(f"  ΔAUC {point:+.4f}  95% CI [{low:+.4f}, {high:+.4f}]")
    positive_years = 0
    for year, g in oos.groupby("year"):
        d = auc(g.y.to_numpy(), g.cross.to_numpy()) - auc(g.y.to_numpy(),
                                                          g.base.to_numpy())
        positive_years += d > 0
        print(f"  {year}: ΔAUC {d:+.4f}   top-quintile hit base "
              f"{top_quintile_hit(g, 'base'):.4f} cross "
              f"{top_quintile_hit(g, 'cross'):.4f}")
    verdict = "PASS" if low > 0 and positive_years >= 3 else "FAIL"
    print(f"  verdict: {verdict} (CI above 0: {low > 0}; "
          f"positive years {positive_years}/4)")
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, choices=sorted(OTHER))
    args = parser.parse_args()
    report(args.symbol, collect(args.symbol))


if __name__ == "__main__":
    main()
