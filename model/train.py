"""LightGBM direction-probability baseline (Sprint 9).

Deliberately boring and replaceable: fixed modest hyperparameters, features
from model/features.py (the same function live inference uses), honest
holdout numbers printed win or lose. Usage:

    python -m model.train --symbol BTCUSDT --interval 1m
"""

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from model.features import DEFAULT_HORIZON_BARS, build_features
from storage.postgres_store import get_price_bars

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
TRAIN_BARS_LIMIT = 100_000
HOLDOUT_FRACTION = 0.2
RANDOM_SEED = 42

_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "num_leaves": 31,
    "max_depth": 6,
    "learning_rate": 0.05,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.9,
    "verbosity": -1,
    "seed": RANDOM_SEED,
    "deterministic": True,
}
_NUM_ROUNDS = 200

logger = logging.getLogger(__name__)


def model_version() -> str:
    """date + short git sha; stamped on artifacts and every signal row."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        ).stdout.strip() or "nogit"
    except OSError:
        sha = "nogit"
    return f"{datetime.now(timezone.utc):%Y%m%d}-{sha}"


def artifact_path(symbol: str, interval: str, version: str) -> Path:
    return ARTIFACTS_DIR / f"{symbol.upper()}_{interval}_{version}.txt"


def latest_artifact(symbol: str, interval: str) -> Path | None:
    """Newest artifact for (symbol, interval) by mtime; None when untrained."""
    candidates = sorted(
        ARTIFACTS_DIR.glob(f"{symbol.upper()}_{interval}_*.txt"),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def train(
    symbol: str,
    interval: str = "1m",
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    bars: pd.DataFrame | None = None,
) -> dict:
    """Train, evaluate on a time-ordered holdout, persist the artifact.
    Returns the metrics dict (also written next to the artifact)."""
    if bars is None:
        bars = pd.DataFrame(get_price_bars(symbol, interval, TRAIN_BARS_LIMIT))
    if bars.empty:
        raise ValueError(f"no stored bars for {symbol} {interval}")

    result = build_features(bars, horizon_bars=horizon_bars)
    if len(result.X) < 200:
        raise ValueError(
            f"not enough feature rows to train ({len(result.X)}); need >= 200"
        )

    # Time-ordered split, never shuffled (walk-forward evaluation lives in
    # model/backtest.py — this holdout is only a sanity gate).
    split = int(len(result.X) * (1 - HOLDOUT_FRACTION))
    # Purge the boundary, exactly as backtest.py does at every fold
    # (`test_start = train_end + horizon_bars`): a label is built from the
    # close `horizon_bars` ahead, so the last horizon_bars-1 training rows
    # would otherwise peek past the split into the holdout this metric is
    # billed as being honest about (KI-004).
    train_end = max(split - horizon_bars, 0)
    X_train, X_hold = result.X.iloc[:train_end], result.X.iloc[split:]
    y_train, y_hold = result.y.iloc[:train_end], result.y.iloc[split:]

    booster = lgb.train(
        _PARAMS, lgb.Dataset(X_train, label=y_train), num_boost_round=_NUM_ROUNDS
    )

    prob = booster.predict(X_hold)
    pred = (prob > 0.5).astype(int)
    eps = 1e-9
    metrics = {
        "symbol": symbol.upper(),
        "interval": interval,
        "horizon_bars": horizon_bars,
        "rows_train": len(X_train),
        "rows_holdout": len(X_hold),
        "holdout_accuracy": float((pred == y_hold).mean()),
        "naive_always_up_accuracy": float(y_hold.mean()),
        "holdout_logloss": float(
            -np.mean(
                y_hold * np.log(prob + eps) + (1 - y_hold) * np.log(1 - prob + eps)
            )
        ),
        "feature_importance": dict(
            zip(result.feature_names,
                booster.feature_importance(importance_type="gain").round(1).tolist())
        ),
    }

    version = model_version()
    metrics["model_version"] = version
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    path = artifact_path(symbol, interval, version)
    booster.save_model(str(path))
    path.with_suffix(".metrics.json").write_text(json.dumps(metrics, indent=2))
    logger.info("Model trained", extra={"artifact": str(path)})
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_BARS)
    args = parser.parse_args()

    metrics = train(args.symbol, args.interval, args.horizon)
    print(json.dumps(metrics, indent=2))
    edge = metrics["holdout_accuracy"] - metrics["naive_always_up_accuracy"]
    verdict = "beats" if edge > 0 else "does NOT beat"
    print(
        f"\n{metrics['symbol']} {metrics['interval']}: model {verdict} the "
        f"always-up baseline ({metrics['holdout_accuracy']:.3f} vs "
        f"{metrics['naive_always_up_accuracy']:.3f}) — honest number, either way."
    )


if __name__ == "__main__":
    main()
