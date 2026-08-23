"""Is the model's probability worth gating on? Walk-forward evidence.

Answers a question the backtest's single `entry_threshold` cannot: does a bar
the model is confident about actually behave differently from one it isn't?
Collects out-of-sample predictions over every walk-forward fold (same fold
shape as `model.backtest`), then reports calibration, ranking skill, the
threshold sweep, and per-position economics.

The 2026-08-22 run is written up in the vault
(`Docs/confidence-gating-analysis.md`); re-run this after a retrain to see
whether the conclusion still holds. Collection is the expensive part (one
LightGBM fit per fold), so it is cached to CSV and reused until --refresh.

Usage:
    python scripts/confidence_report.py --symbol BTCUSDT
    python scripts/confidence_report.py --symbol ETHUSDT --interval 1m --refresh
"""

import argparse
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.backtest import BacktestConfig, find_episodes  # noqa: E402
from model.features import build_features  # noqa: E402
from model.train import _NUM_ROUNDS, _PARAMS, TRAIN_BARS_LIMIT  # noqa: E402
from storage.postgres_store import get_price_bars  # noqa: E402

# NOTE — this script shares `find_episodes` with the harness but deliberately
# stops there. It does NOT apply `merge_overlapping_holds` or `apply_cooldown`,
# because it measures the *selection value of a threshold* per episode, not the
# equity path one unit of capital would have taken. So its `positions` counts
# are episode counts and are not comparable to `run_backtest`'s
# `total_positions`, which merges overlapping holds (KI-001) and, when
# `cooldown_bars > 0`, declines re-entries. Anything that needs the equity path
# must call `run_backtest` rather than extending the loops below.
CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
BPS = 1e4
_BUCKET_EDGES = (0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0)
_THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)


def round_trip_cost(config: BacktestConfig) -> float:
    """What one position costs to open and close — the hurdle every number
    here is measured against."""
    return 2 * (config.fee_per_side + config.slippage)


def _forward_returns(
    bars: pd.DataFrame, index: pd.Index, horizon_bars: int
) -> np.ndarray:
    """Per-BAR forward return over the label horizon — what the bucket tables
    measure. The equity path in `model/backtest.py` deliberately does not use
    this: it prices whole positions entry-close to exit-close (KI-001/KI-040),
    and a per-bar return there is what compounded overlapping holds.
    """
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    close = frame["close"].astype(float).sort_index()
    fwd = close.shift(-horizon_bars) / close - 1.0
    return fwd.loc[index].to_numpy()


def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Rank-based ROC AUC. 0.5 is a coin flip; NaN when one class is absent."""
    y = np.asarray(y)
    p = np.asarray(p)
    n_pos, n_neg = int((y == 1).sum()), int((y != 1).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = pd.Series(p).rank().to_numpy()
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def _deflated_t(net: np.ndarray, horizon: int) -> float:
    """t-statistic with the effective sample size cut by the overlap between
    consecutive rows — each bar's forward return shares `horizon` bars with
    its neighbour, so the naive t is inflated."""
    if len(net) < 2:
        return float("nan")
    eff_n = max(len(net) / horizon, 1.0)
    se = net.std(ddof=1) / np.sqrt(eff_n)
    return float(net.mean() / se) if se > 0 else float("nan")


def collect_oos(bars: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """Walk forward over the bars, refitting per fold, and keep every
    out-of-sample prediction: one row per test bar."""
    feats = build_features(bars, horizon_bars=config.horizon_bars)
    X, y = feats.X, feats.y
    forward = pd.Series(
        _forward_returns(bars, X.index, config.horizon_bars), index=X.index
    )
    fold_span = config.train_rows + config.horizon_bars + config.test_rows
    if len(X) < fold_span:
        raise ValueError(f"not enough rows for one fold: have {len(X)}, need {fold_span}")

    chunks, start, fold = [], 0, 0
    while start + fold_span <= len(X):
        train_end = start + config.train_rows
        test_start = train_end + config.horizon_bars  # purge, as model.backtest
        test_end = test_start + config.test_rows
        booster = lgb.train(
            _PARAMS,
            lgb.Dataset(X.iloc[start:train_end], label=y.iloc[start:train_end]),
            num_boost_round=_NUM_ROUNDS,
        )
        index = X.index[test_start:test_end]
        chunks.append(pd.DataFrame({
            "ts": index,
            "fold": fold,
            "p": booster.predict(X.iloc[test_start:test_end]),
            "y": y.iloc[test_start:test_end].to_numpy(),
            "fwd": forward.loc[index].to_numpy(),
        }))
        start += config.test_rows
        fold += 1
        if fold % 25 == 0:
            print(f"  fold {fold} ...", flush=True)
    return pd.concat(chunks, ignore_index=True)


def bucket_table(oos: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """Realized behaviour per predicted-probability bucket. A model whose p
    means anything shows a rising `realized` column."""
    cost = round_trip_cost(config)
    rows = []
    for lo, hi in zip(_BUCKET_EDGES[:-1], _BUCKET_EDGES[1:]):
        sub = oos[(oos.p >= lo) & (oos.p < hi)] if hi < 1.0 else oos[oos.p >= lo]
        if sub.empty:
            continue
        rows.append({
            "bucket": f"{lo:.2f}-{hi:.2f}",
            "n": len(sub),
            "pct_rows": 100 * len(sub) / len(oos),
            "pred_p": sub.p.mean(),
            "realized": sub.y.mean(),
            "gross_bp": sub.fwd.mean() * BPS,
            "net_bp": (sub.fwd.mean() - cost) * BPS,
            "t": _deflated_t((sub.fwd - cost).to_numpy(), config.horizon_bars),
        })
    return pd.DataFrame(rows)


def threshold_table(oos: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """The `entry_threshold` knob swept, counting both the bars the current
    code charges fees for and the positions a real execution would open."""
    cost = round_trip_cost(config)
    rows = []
    for thr in _THRESHOLDS:
        sub = oos[oos.p > thr]
        if sub.empty:
            continue
        episodes = find_episodes((oos.p > thr).to_numpy())
        rows.append({
            "thr": thr,
            "in_market_bars": len(sub),
            "positions": len(episodes),
            "fee_overcharge": len(sub) / len(episodes) if episodes else float("nan"),
            "hit": sub.y.mean(),
            "gross_bp": sub.fwd.mean() * BPS,
            "net_bp": (sub.fwd.mean() - cost) * BPS,
            "t": _deflated_t((sub.fwd - cost).to_numpy(), config.horizon_bars),
        })
    return pd.DataFrame(rows)


def position_table(
    oos: pd.DataFrame, closes: pd.Series, config: BacktestConfig
) -> pd.DataFrame:
    """Per-position economics: one entry, one exit, one round trip.

    Episodes are priced independently here, overlaps included — this is a
    *mean* over positions, which overlap does not bias, and it answers "what
    does a position at this threshold earn". `run_backtest`'s equity curve
    asks a different question (what one unit of capital compounds), so it
    merges overlapping holds; the two are not meant to agree row for row.
    """
    cost = round_trip_cost(config)
    horizon = config.horizon_bars
    position = {ts: i for i, ts in enumerate(closes.index)}
    values = closes.to_numpy()
    rows = []
    for thr in _THRESHOLDS:
        returns, holds = [], []
        for start, end in find_episodes((oos.p > thr).to_numpy()):
            entry = position.get(oos.ts.iloc[start])
            exit_ = position.get(oos.ts.iloc[end])
            if entry is None or exit_ is None or exit_ + horizon >= len(values):
                continue
            returns.append(values[exit_ + horizon] / values[entry] - 1.0)
            holds.append(end - start + 1 + horizon)
        if not returns:
            continue
        gross = np.array(returns)
        net = gross - cost
        se = net.std(ddof=1) / np.sqrt(len(net)) if len(net) > 1 else float("nan")
        rows.append({
            "thr": thr,
            "positions": len(gross),
            "avg_hold_bars": float(np.mean(holds)),
            "entry_bars": float(np.mean(holds)) - horizon,
            "clearing_cost": float((net > 0).mean()),
            "gross_bp": float(gross.mean() * BPS),
            "net_bp": float(net.mean() * BPS),
            "t": float(net.mean() / se) if se and se > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


def within_fold_lift(oos: pd.DataFrame, decile: float = 0.1) -> dict:
    """Does the model rank bars *inside* a fold, where its p-scale is at least
    self-consistent? Compares each fold's top decile against that fold's mean,
    which a global threshold cannot see."""
    def _lift(group: pd.DataFrame) -> pd.Series:
        k = max(int(len(group) * decile), 1)
        return pd.Series({
            "top": group.nlargest(k, "p").fwd.mean(),
            "all": group.fwd.mean(),
        })

    per_fold = oos.groupby("fold")[["p", "fwd"]].apply(_lift)
    diff = (per_fold["top"] - per_fold["all"]).dropna()
    aucs = oos.groupby("fold")[["y", "p"]].apply(
        lambda g: auc(g.y.to_numpy(), g.p.to_numpy())
    ).dropna()
    above = int((aucs > 0.5).sum())
    se = diff.std(ddof=1) / np.sqrt(len(diff)) if len(diff) > 1 else float("nan")
    return {
        "folds": len(diff),
        "lift_bp": float(diff.mean() * BPS),
        "t": float(diff.mean() / se) if se and se > 0 else float("nan"),
        # a fold whose test window is all one direction has no AUC at all
        "median_fold_auc": float(aucs.median()) if len(aucs) else float("nan"),
        "folds_above_half": above,
        "sign_test_z": (
            float((above - len(aucs) / 2) / np.sqrt(len(aucs) * 0.25))
            if len(aucs) else float("nan")
        ),
    }


def selection_value(oos: pd.DataFrame, threshold: float) -> float:
    """Edge of the gated bars over simply being long every bar, in bps. This
    is what the gate is worth — not the gated bars' return on its own."""
    gated = oos[oos.p > threshold]
    if gated.empty:
        return float("nan")
    return float((gated.fwd.mean() - oos.fwd.mean()) * BPS)


def _print_report(oos: pd.DataFrame, closes: pd.Series, config: BacktestConfig,
                  label: str) -> None:
    cost = round_trip_cost(config)
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(f"OOS rows: {len(oos):,}   folds: {oos.fold.nunique()}   "
          f"span: {oos.ts.min():%Y-%m-%d} -> {oos.ts.max():%Y-%m-%d}")
    base = oos.y.mean()
    print(f"base rate P(up over {config.horizon_bars} bars): {base:.4f}   "
          f"round-trip cost: {cost * BPS:.0f} bps")
    print(f"predicted p: min {oos.p.min():.3f}  median {oos.p.median():.3f}  "
          f"max {oos.p.max():.3f}")

    print(f"\n-- P(up) buckets: does confidence track accuracy? --")
    print(bucket_table(oos, config).to_string(
        index=False, float_format=lambda v: f"{v:8.3f}"))

    print(f"\n-- entry_threshold sweep (long-only, as model/backtest.py) --")
    print(threshold_table(oos, config).to_string(
        index=False, float_format=lambda v: f"{v:8.3f}"))

    print(f"\n-- per position (one round trip each, correcting KI-040) --")
    print(position_table(oos, closes, config).to_string(
        index=False, float_format=lambda v: f"{v:8.3f}"))

    for thr in (0.80, 0.90):
        print(f"\nselection value @p>{thr:.2f}: {selection_value(oos, thr):+.2f} bps "
              f"over being long every bar ({oos.fwd.mean() * BPS:+.2f} bps)")

    lift = within_fold_lift(oos)
    print(f"\nwithin-fold top decile vs fold mean: {lift['lift_bp']:+.2f} bps "
          f"(t={lift['t']:+.2f}, {lift['folds']} folds)")
    print(f"per-fold AUC: median {lift['median_fold_auc']:.4f}, "
          f"{lift['folds_above_half']}/{lift['folds']} above 0.50 "
          f"(sign test z={lift['sign_test_z']:+.2f})")
    print("  ^ significances are upper bounds: adjacent folds share most of "
          "their training rows")

    pooled = auc(oos.y.to_numpy(), oos.p.to_numpy())
    brier = float(((oos.p - oos.y) ** 2).mean())
    brier_base = float(((base - oos.y) ** 2).mean())
    accuracy = float(((oos.p >= 0.5).astype(int) == oos.y).mean())
    print(f"\npooled AUC: {pooled:.4f}   accuracy @0.5: {accuracy:.4f}   "
          f"best constant predictor: {max(base, 1 - base):.4f}")
    print(f"Brier: {brier:.4f}   base-rate Brier: {brier_base:.4f}   "
          f"skill score: {1 - brier / brier_base:+.4f}")

    absolute = oos.fwd.abs()
    print(f"|{config.horizon_bars}-bar move|: median {absolute.median() * BPS:.1f} bps, "
          f"mean {absolute.mean() * BPS:.1f} bps; "
          f"P(|move| > {cost * BPS:.0f} bps cost) = {(absolute > cost).mean():.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--cache", type=Path, default=None,
                        help="collected OOS predictions (default: model/artifacts/)")
    parser.add_argument("--refresh", action="store_true",
                        help="recollect even when the cache exists")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    config = BacktestConfig()
    cache = args.cache or CACHE_DIR / f"oos_{symbol}_{args.interval}.csv"
    bars = pd.DataFrame(get_price_bars(symbol, args.interval, TRAIN_BARS_LIMIT))
    if bars.empty:
        print(f"no stored bars for {symbol} {args.interval}")
        raise SystemExit(1)

    if cache.exists() and not args.refresh:
        oos = pd.read_csv(cache, parse_dates=["ts"])
        print(f"reusing {len(oos):,} cached predictions from {cache}")
    else:
        print(f"{symbol}: {len(bars)} bars -> collecting walk-forward predictions")
        oos = collect_oos(bars, config)
        cache.parent.mkdir(parents=True, exist_ok=True)
        oos.to_csv(cache, index=False)
        print(f"cached {len(oos):,} predictions -> {cache}")

    frame = bars.copy()
    frame["ts"] = pd.to_datetime(frame["timestamp"], utc=True)
    closes = frame.set_index("ts")["close"].astype(float).sort_index()
    _print_report(oos, closes, config, f"{symbol} {args.interval} — walk-forward OOS")


if __name__ == "__main__":
    main()
