"""One-shot live inference (Sprint 9): load the latest artifact for a
symbol, build features for the newest complete bar, write one signal row.
Sprint 10 wraps this in a scheduled cadence job. Usage:

    python -m model.predict --symbol BTCUSDT --interval 1m
"""

import argparse
import json
import logging

import lightgbm as lgb
import pandas as pd

from model.features import DEFAULT_HORIZON_BARS, build_latest_features
from model.train import latest_artifact
from storage.postgres_store import get_price_bars
from storage.writes import write_signals

# Enough history for the longest warm-up window plus slack.
PREDICT_BARS_LIMIT = 200

logger = logging.getLogger(__name__)


class NoModelArtifact(Exception):
    """No trained model exists for this symbol/interval — callers may skip."""


def predict(
    symbol: str,
    interval: str = "1m",
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    bars: pd.DataFrame | None = None,
) -> dict | None:
    """Predict for the newest complete bar and persist the signal.
    Returns the signal dict, or None when history is too short."""
    artifact = latest_artifact(symbol, interval)
    if artifact is None:
        raise NoModelArtifact(f"no trained model for {symbol} {interval}")
    # model_version is embedded in the artifact filename: SYMBOL_interval_version.txt
    version = artifact.stem.split("_", 2)[2]

    if bars is None:
        bars = pd.DataFrame(get_price_bars(symbol, interval, PREDICT_BARS_LIMIT))
    if bars.empty:
        return None
    features = build_latest_features(bars)
    if features is None:
        logger.info(
            "Not enough history to predict", extra={"symbol": symbol}
        )
        return None

    booster = lgb.Booster(model_file=str(artifact))
    probability_up = float(booster.predict(features)[0])

    signal = {
        "symbol": symbol.upper(),
        "interval": interval,
        "signal_timestamp": features.index[-1].to_pydatetime(),
        "model_version": version,
        "horizon_bars": horizon_bars,
        "direction": "up" if probability_up >= 0.5 else "down",
        "probability": probability_up if probability_up >= 0.5 else 1 - probability_up,
    }
    write_signals([signal])
    logger.info("Signal written", extra={k: str(v) for k, v in signal.items()})
    return signal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    args = parser.parse_args()

    signal = predict(args.symbol, args.interval)
    print(json.dumps(signal, indent=2, default=str) if signal else "no signal")


if __name__ == "__main__":
    main()
