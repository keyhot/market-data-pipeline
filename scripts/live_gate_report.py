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
CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"

# E6 (vault Docs/model-improvement-plan.md "Current plan v2"): every
# candidate since L1 cuts bars at 2026-01-01 and 2026 is read at most once,
# never for selection. Extracted to its own function (rather than inlined in
# main()) so collect_cached() enforces it too, not only the CLI path.
SEALED_HOLDOUT_START = pd.Timestamp("2026-01-01", tz="UTC")


def auc(y: np.ndarray, p: np.ndarray) -> float:
    ranks = pd.Series(p).rank().to_numpy()
    pos = y == 1
    n_pos, n_neg = pos.sum(), (~pos).sum()
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def exclude_sealed_holdout(bars: pd.DataFrame) -> pd.DataFrame:
    """Drop every bar at or after the sealed 2026 holdout (E6). Both the CLI
    path and collect_cached() funnel through here so nothing can read the
    sealed window by skipping main()."""
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    return bars[stamps < SEALED_HOLDOUT_START].reset_index(drop=True)


def collect(bars: pd.DataFrame, test_years=TEST_YEARS) -> pd.DataFrame:
    feats = build_features(bars, horizon_bars=HORIZON)
    X, y = feats.X, feats.y
    years = X.index.year
    chunks = []
    for year in test_years:
        test = np.flatnonzero(years == year)
        train_end = test[0] - HORIZON  # purge: the last label peeks one bar
        booster = lgb.train(
            _PARAMS, lgb.Dataset(X.iloc[:train_end], label=y.iloc[:train_end]),
            num_boost_round=_NUM_ROUNDS,
        )
        chunks.append(pd.DataFrame({
            "ts": X.index[test],
            "year": year,
            "fold": year,
            "p": booster.predict(X.iloc[test]),
            "y": y.iloc[test].to_numpy(),
        }))
        print(f"  {year}: trained on {train_end:,} rows, tested {len(test):,}",
              flush=True)
    return pd.concat(chunks, ignore_index=True)


def collect_cached(
    bars: pd.DataFrame, cache: Path, test_years=TEST_YEARS, refresh: bool = False,
) -> pd.DataFrame:
    """Cache collect()'s per-row OOS predictions to `cache` (columns
    ts/year/fold/p/y — the same shape scripts/confidence_report.py's
    oos_{symbol}_{interval}.csv cache uses, so
    scripts/calibration_report.py reads either unmodified). Refitting
    LightGBM per test year is the expensive part; reused across runs until
    `refresh=True`."""
    if cache.exists() and not refresh:
        return pd.read_csv(cache, parse_dates=["ts"])
    oos = collect(bars, test_years=test_years)
    cache.parent.mkdir(parents=True, exist_ok=True)
    oos.to_csv(cache, index=False)
    return oos


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
    parser.add_argument(
        "--cache", type=Path, default=None,
        help="cache per-row OOS predictions here (default: "
             "model/artifacts/oos_{symbol}_15m_h1.csv); reused until --refresh",
    )
    parser.add_argument("--refresh", action="store_true",
                        help="recollect even when the cache exists")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    cache = args.cache or CACHE_DIR / f"oos_{symbol}_15m_h1.csv"

    bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
    bars = exclude_sealed_holdout(bars)  # E6: 2026 is never read here
    print(f"{symbol}: {len(bars):,} 15m bars through 2025")
    oos = collect_cached(bars, cache, refresh=args.refresh)
    print(f"({'refreshed' if args.refresh else 'cached/loaded'} -> {cache})")
    report(oos)


if __name__ == "__main__":
    main()
