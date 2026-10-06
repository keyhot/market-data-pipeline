"""Apply a probability calibrator to a model's raw p (2026-10-06).

Runtime-safe by construction: numpy and json only, no scikit-learn here or
anywhere else under model/, api/, world/, director/ — the calibrator is
*fitted* offline (scikit-learn lives in the dev dependency group only;
scripts/calibration_report.py does that fitting and is never imported by the
running service) and shipped as a tiny JSON artifact that this module reads
and applies with closed-form math.

Fail-open: a missing artifact, a version mismatch, or a malformed/invalid
artifact (e.g. a Platt slope that would invert the ranking) all fall back to
the raw probability rather than raising — a calibration artifact must never
be able to break inference.
"""

import json
import logging
from pathlib import Path

import numpy as np

from model import train as train_module

logger = logging.getLogger(__name__)


def calibrator_path(symbol: str, interval: str) -> Path:
    """model/artifacts/calibrator_{SYMBOL}_{interval}.json — one calibrator
    file per (symbol, signal interval), analogous to confidence_report's
    oos_{symbol}_{interval}.csv cache it was fitted from. Read via
    `train_module.ARTIFACTS_DIR` (a module attribute, not a bound import) so
    tests that monkeypatch `train.ARTIFACTS_DIR` redirect this lookup too.
    """
    return train_module.ARTIFACTS_DIR / f"calibrator_{symbol.upper()}_{interval}.json"


def load_calibrator(symbol: str, interval: str, model_version: str) -> dict | None:
    """The artifact for this (symbol, interval), only if it says it calibrates
    this exact model_version. None on any mismatch, missing file, or bad
    JSON — callers must treat None as "use raw p"."""
    path = calibrator_path(symbol, interval)
    if not path.exists():
        logger.warning(
            "No calibrator artifact, publishing raw p",
            extra={"symbol": symbol, "interval": interval,
                   "model_version": model_version},
        )
        return None
    try:
        artifact = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Calibrator artifact unreadable, falling back to raw p",
            extra={"symbol": symbol, "interval": interval, "error": str(exc)},
        )
        return None
    if artifact.get("model_version") != model_version:
        logger.warning(
            "Calibrator model_version mismatch, falling back to raw p",
            extra={
                "symbol": symbol,
                "interval": interval,
                "artifact_version": artifact.get("model_version"),
                "live_version": model_version,
            },
        )
        return None
    return artifact


def apply_calibrator(p: float, artifact: dict) -> float:
    """Map raw p through the fitted calibrator. Returns the raw p unchanged
    if the artifact is malformed in a way that could invert the ranking
    (fail-open applies to a bad artifact exactly as it does to a missing
    one) — never raises."""
    method = artifact.get("method")
    try:
        if method == "platt":
            a, b = float(artifact["a"]), float(artifact["b"])
            if a < 0:  # would invert calibrated p relative to raw p
                raise ValueError(f"platt slope a={a} < 0, would invert ranking")
            clipped = np.clip(p, 1e-6, 1 - 1e-6)
            logit = np.log(clipped / (1 - clipped))
            return float(1.0 / (1.0 + np.exp(-(a * logit + b))))
        if method == "isotonic":
            x = np.asarray(artifact["x"], dtype=float)
            y = np.asarray(artifact["y"], dtype=float)
            if len(x) == 0 or len(x) != len(y):
                raise ValueError("isotonic x/y empty or length-mismatched")
            return float(np.interp(p, x, y))
        raise ValueError(f"unknown calibration method {method!r}")
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Calibrator artifact malformed, falling back to raw p",
            extra={"method": method, "error": str(exc)},
        )
        return float(p)
