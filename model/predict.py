"""One-shot live inference (Sprint 9): load the latest artifact for a
symbol, build features for the newest complete bar, write one signal row.
Sprint 10 wraps this in a scheduled cadence job.

Since 2026-09-27 the on-air 1m stream is served by a model trained on years
of 15m bars (the 1m artifact it replaced had seen 21 hours): each minute the
newest 1m bars are rolled into 15m bars ending now, and the model predicts the
next one — still a 1m signal, 15 minutes ahead, so the resolver, charts and
room are unchanged. It runs every minute but publishes only when the call is
confident (`GATE_QUANTILE` in model/train.py) and no earlier call on the
symbol is still open; the rest are held. Usage:

    python -m model.predict --symbol BTCUSDT --interval 1m
"""

import argparse
import json
import logging

import lightgbm as lgb
import pandas as pd

from model.calibration import apply_calibrator, load_calibrator
from model.features import DEFAULT_HORIZON_BARS, build_latest_features, roll_bars
from model.train import latest_artifact, latest_metrics
from storage.postgres_store import get_price_bars, get_signals
from storage.writes import write_signals

# Enough history for the longest warm-up window plus slack.
PREDICT_BARS_LIMIT = 200

# Signal interval -> (the model interval that serves it, minutes rolled per
# model bar). An interval not listed is served by its own model, unrolled.
SERVED_BY = {"1m": ("15m", 15)}

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
    Returns the signal dict; {"held": reason} when the call is withheld
    ("call_open" or "low_confidence"); None when history is too short."""
    model_interval, rolled = SERVED_BY.get(interval, (interval, None))
    artifact = latest_artifact(symbol, model_interval)
    if artifact is None:
        raise NoModelArtifact(f"no trained model for {symbol} {model_interval}")
    # model_version is embedded in the artifact filename: SYMBOL_interval_version.txt
    version = artifact.stem.split("_", 2)[2]

    if bars is None:
        # Each rolled bar needs `rolled` minutes, and the warm-up needs 60 bars.
        limit = PREDICT_BARS_LIMIT * (rolled or 1)
        bars = pd.DataFrame(get_price_bars(symbol, interval, limit))
    if bars.empty:
        return None
    features = build_latest_features(roll_bars(bars, rolled) if rolled else bars)
    if features is None:
        logger.info(
            "Not enough history to predict", extra={"symbol": symbol}
        )
        return None
    now = features.index[-1]

    if _call_open(symbol, interval, now):
        return {"held": "call_open"}

    booster = lgb.Booster(model_file=str(artifact))
    probability_up = _probability_up(booster, features)
    metrics = latest_metrics(symbol, model_interval) or {}
    if abs(probability_up - 0.5) < metrics.get("confidence_cutoff", 0.0):
        logger.debug("Coin flip held", extra={"symbol": symbol,
                                               "p": round(probability_up, 4)})
        return {"held": "low_confidence"}

    # Gating and direction above are decided on raw p — the holdout cutoff
    # was measured against raw p's own distribution. Only the PUBLISHED
    # probability is calibrated, so it means "how often does this actually
    # resolve", not just "which side". Fail-open: no artifact, or one that
    # doesn't match this exact model_version, means raw p ships unchanged.
    direction = "up" if probability_up >= 0.5 else "down"
    probability = probability_up if direction == "up" else 1 - probability_up
    calibrator = load_calibrator(symbol, interval, version)
    if calibrator is not None:
        calibrated_up = apply_calibrator(probability_up, calibrator)
        probability = calibrated_up if direction == "up" else 1 - calibrated_up

    signal = {
        "symbol": symbol.upper(),
        "interval": interval,
        "signal_timestamp": now.to_pydatetime(),
        "model_version": version,
        "horizon_bars": horizon_bars,
        "direction": direction,
        "probability": probability,
    }
    write_signals([signal])
    logger.info("Signal written", extra={k: str(v) for k, v in signal.items()})
    return signal


def _probability_up(booster: lgb.Booster, features: pd.DataFrame) -> float:
    return float(booster.predict(features)[0])


def _call_open(symbol: str, interval: str, now: pd.Timestamp) -> bool:
    """True while the newest published call on this stream awaits its horizon:
    consecutive rolled bars share 14 of 15 minutes, so a confident stretch
    would otherwise publish the same call once a minute."""
    delta = pd.Timedelta(interval)
    for last in get_signals(symbol, interval, limit=1):
        opened = pd.Timestamp(last["signal_timestamp"])
        if now < opened + delta * last["horizon_bars"]:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    args = parser.parse_args()

    signal = predict(args.symbol, args.interval)
    print(json.dumps(signal, indent=2, default=str) if signal else "no signal")


if __name__ == "__main__":
    main()
