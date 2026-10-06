"""Is the model's probability p honest, and can a calibrator fix it?
(2026-10-06)

Evaluates three ways to turn raw p into an honest probability — Platt
(logistic regression on logit(p)), isotonic (sklearn IsotonicRegression,
out_of_bounds="clip"), and a constant base-rate predictor — against raw p
itself, on the SAME out-of-sample rows for all four. The comparison is
fold-honest: for fold k, every calibrator is fit on folds < k only (an
expanding window) and applied to fold k, exactly mirroring how it would be
used live (fit on the past, apply to the future). Fold 0 has no "past" to fit
on, so it is excluded from every method's rows — the comparison is over the
same subset for all four.

This script is dev tooling only: scikit-learn (dev dependency group) is
imported here and nowhere under model/, api/, world/, director/ at runtime.
It reads the cached walk-forward OOS predictions `scripts/confidence_report.py
--refresh` already collected (oos_{symbol}_{interval}.csv in model/artifacts/)
rather than recollecting — this script never trains a LightGBM booster.

Also fits the winning method on ALL cached rows (not fold-expanding — that
split was only to measure honestly) and writes the production artifact,
calibrator_{symbol}_{interval}.json, next to the OOS cache.

    poetry run python scripts/calibration_report.py --symbol BTCUSDT
    poetry run python scripts/calibration_report.py --symbol BTCUSDT --write
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.calibration import apply_calibrator  # noqa: E402
from scripts.confidence_report import auc, log_loss_report  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
_EPS = 1e-6


def _logit(p: np.ndarray) -> np.ndarray:
    clipped = np.clip(p, _EPS, 1 - _EPS)
    return np.log(clipped / (1 - clipped))


def _fit_platt(p: np.ndarray, y: np.ndarray) -> dict | None:
    """None only when there isn't enough class variety to fit at all (can't
    even estimate an intercept). A fitted slope a < 0 — the unconstrained MLE
    inverting the ranking, which happens on small/extreme-tailed folds when a
    model has no real rank information — is constrained to intercept-only
    (a=0, b=logit(train mean)) rather than discarded: that is what "this
    slope carries no information, fall back to the base rate" means at FIT
    time. `model.calibration.apply_calibrator`'s a < 0 guard is a separate,
    defensive check against a malformed artifact at APPLY time; a correctly
    fit artifact should never trip it."""
    if len(np.unique(y)) < 2:
        return None
    lr = LogisticRegression(C=np.inf)
    lr.fit(_logit(p).reshape(-1, 1), y)
    a, b = float(lr.coef_[0][0]), float(lr.intercept_[0])
    if a < 0:
        a, b = 0.0, float(_logit(np.array([y.mean()]))[0])
    return {"method": "platt", "a": a, "b": b}


def _fit_isotonic(p: np.ndarray, y: np.ndarray) -> dict | None:
    if len(np.unique(y)) < 2:
        return None
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p, y)
    return {
        "method": "isotonic",
        "x": iso.X_thresholds_.tolist(),
        "y": iso.y_thresholds_.tolist(),
    }


def expanding_calibrate(oos: pd.DataFrame) -> pd.DataFrame:
    """Per-fold expanding calibration: fold k is calibrated using only folds
    < k. Fold 0 is dropped (no prior data for any method). Returns one row
    per evaluated bar with columns raw/platt/isotonic/base alongside y."""
    folds = sorted(oos.fold.unique())
    rows = []
    for k in folds[1:]:
        train = oos[oos.fold < k]
        test = oos[oos.fold == k]
        p_train, y_train = train.p.to_numpy(), train.y.to_numpy()
        p_test = test.p.to_numpy()

        platt = _fit_platt(p_train, y_train)
        iso = _fit_isotonic(p_train, y_train)
        base_rate = float(y_train.mean())

        rows.append(pd.DataFrame({
            "fold": k,
            "y": test.y.to_numpy(),
            "raw": p_test,
            "platt": (
                np.array([apply_calibrator(p, platt) for p in p_test])
                if platt else p_test
            ),
            "isotonic": (
                np.array([apply_calibrator(p, iso) for p in p_test])
                if iso else p_test
            ),
            "base": base_rate,
        }))
    return pd.concat(rows, ignore_index=True)


def _metrics(y: np.ndarray, p: np.ndarray) -> dict:
    ll = log_loss_report(y, p)
    brier = float(((p - y) ** 2).mean())
    brier_base = float(((y.mean() - y) ** 2).mean())
    return {
        "log_loss": ll["log_loss"],
        "log_loss_skill": ll["log_loss_skill"],
        "brier_skill": 1 - brier / brier_base if brier_base else float("nan"),
        "auc": auc(y, p),
    }


def report(oos: pd.DataFrame, symbol: str, interval: str) -> pd.DataFrame:
    calibrated = expanding_calibrate(oos)
    y = calibrated.y.to_numpy()
    rows = [
        {"method": m, **_metrics(y, calibrated[m].to_numpy())}
        for m in ("raw", "platt", "isotonic", "base")
    ]
    table = pd.DataFrame(rows)
    print(f"\n{'=' * 70}\n{symbol} {interval} — calibration (expanding, "
          f"{len(calibrated):,} rows, folds {calibrated.fold.min()}-"
          f"{calibrated.fold.max()})\n{'=' * 70}")
    print(table.to_string(index=False, float_format=lambda v: f"{v:8.4f}"))
    return table


def fit_full_sample(oos: pd.DataFrame, method: str) -> dict:
    """Fit the chosen method on every cached OOS row (not fold-expanding —
    that split exists only to measure honestly in `report`)."""
    p, y = oos.p.to_numpy(), oos.y.to_numpy()
    if method == "platt":
        artifact = _fit_platt(p, y) or {"method": "platt", "a": 0.0,
                                         "b": float(_logit(np.array([y.mean()]))[0])}
    elif method == "isotonic":
        artifact = _fit_isotonic(p, y)
    else:
        raise ValueError(f"unknown method {method!r}")
    return artifact


# Selection rule: pick the real candidate (Platt or isotonic — raw is never
# shippable, base is the no-skill null, not a calibrator) with the lowest
# fold-expanding log loss. When BOTH real candidates land within 5%
# (relative) of the constant base-rate predictor's log loss — i.e. the
# honest answer is "neither beats guessing the rate" — ship Platt instead of
# whichever edges it out by noise, for simplicity.
_BASE_RATE_TOLERANCE = 0.05


def _pick_method(table: pd.DataFrame) -> str:
    base_ll = float(table.loc[table.method == "base", "log_loss"].iloc[0])
    real = table[table.method.isin(("platt", "isotonic"))]
    within_tolerance = (
        (real.log_loss - base_ll).abs() / base_ll <= _BASE_RATE_TOLERANCE
    )
    if within_tolerance.all():
        return "platt"
    return str(real.loc[real.log_loss.idxmin(), "method"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--cache", type=Path, default=None,
                        help="OOS predictions cache to read (default: "
                             "model/artifacts/oos_{symbol}_{interval}.csv)")
    parser.add_argument("--method", choices=("platt", "isotonic"), default=None,
                        help="skip the pick-lowest-log-loss rule, force this method")
    parser.add_argument("--write", action="store_true",
                        help="write the fitted artifact (JSON) next to the cache")
    parser.add_argument("--model-version", default=None,
                        help="model_version to stamp the artifact with; required "
                             "with --write")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    cache = args.cache or CACHE_DIR / f"oos_{symbol}_{args.interval}.csv"
    if not cache.exists():
        print(f"no cached OOS predictions at {cache} "
              f"(run scripts/confidence_report.py --symbol {symbol} first)")
        raise SystemExit(1)
    oos = pd.read_csv(cache, parse_dates=["ts"])

    table = report(oos, symbol, args.interval)
    method = args.method or _pick_method(table)
    print(f"\nchosen method: {method}")

    if args.write:
        if not args.model_version:
            print("--write requires --model-version")
            raise SystemExit(1)
        artifact = fit_full_sample(oos, method)
        artifact["model_version"] = args.model_version
        artifact["symbol"] = symbol
        artifact["interval"] = args.interval
        artifact["fitted_on_rows"] = int(len(oos))
        out = CACHE_DIR / f"calibrator_{symbol}_{args.interval}.json"
        out.write_text(json.dumps(artifact, indent=2))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
