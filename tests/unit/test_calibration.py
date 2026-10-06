import json

import pytest

from model import train as train_module
from model.calibration import apply_calibrator, calibrator_path, load_calibrator


@pytest.fixture(autouse=True)
def tmp_artifacts(tmp_path, monkeypatch):
    # model.calibration reads train_module.ARTIFACTS_DIR at call time, so
    # redirecting it here redirects calibrator lookups too.
    monkeypatch.setattr(train_module, "ARTIFACTS_DIR", tmp_path)
    return tmp_path


def _write(path_dir, symbol, interval, artifact):
    path = path_dir / f"calibrator_{symbol.upper()}_{interval}.json"
    path.write_text(json.dumps(artifact))
    return path


# -- apply_calibrator: platt --------------------------------------------

def test_apply_platt_matches_closed_form_sigmoid():
    artifact = {"method": "platt", "a": 1.0, "b": 0.0}
    # a=1, b=0 is the identity transform on logit(p), so calibrated == raw.
    assert apply_calibrator(0.7, artifact) == pytest.approx(0.7, abs=1e-6)


def test_apply_platt_shrinks_toward_half_with_shallow_slope():
    artifact = {"method": "platt", "a": 0.1, "b": 0.0}
    calibrated = apply_calibrator(0.95, artifact)
    assert 0.5 < calibrated < 0.95


def test_apply_platt_zero_slope_is_valid_not_a_fallback():
    """a == 0 is a legitimate (if degenerate) calibration — a flat line at
    the fitted intercept, not an inversion. It must NOT hit the a < 0
    fallback: this pins the guard at `< 0`, not `<= 0`."""
    artifact = {"method": "platt", "a": 0.0, "b": 0.0}
    assert apply_calibrator(0.9, artifact) == pytest.approx(0.5)
    assert apply_calibrator(0.1, artifact) == pytest.approx(0.5)


def test_apply_platt_negative_slope_falls_back_to_raw():
    """A fitted slope a < 0 would invert the ranking (higher raw p -> lower
    calibrated p) — the one mutation that must survive: flip `a < 0` to
    `a <= 0` or drop the guard entirely and this test catches it."""
    artifact = {"method": "platt", "a": -0.5, "b": 0.0}
    assert apply_calibrator(0.9, artifact) == pytest.approx(0.9)


# -- apply_calibrator: isotonic -----------------------------------------

def test_apply_isotonic_interpolates_between_knots():
    artifact = {"method": "isotonic", "x": [0.0, 0.5, 1.0], "y": [0.1, 0.5, 0.9]}
    assert apply_calibrator(0.25, artifact) == pytest.approx(0.3)


def test_apply_isotonic_clips_outside_knot_range():
    artifact = {"method": "isotonic", "x": [0.2, 0.8], "y": [0.3, 0.7]}
    assert apply_calibrator(0.0, artifact) == pytest.approx(0.3)
    assert apply_calibrator(1.0, artifact) == pytest.approx(0.7)


def test_apply_isotonic_malformed_knots_falls_back_to_raw():
    artifact = {"method": "isotonic", "x": [0.0, 0.5], "y": [0.1]}  # mismatched
    assert apply_calibrator(0.6, artifact) == pytest.approx(0.6)


def test_apply_calibrator_unknown_method_falls_back_to_raw():
    assert apply_calibrator(0.42, {"method": "nonsense"}) == pytest.approx(0.42)


# -- load_calibrator: missing / version mismatch ------------------------

def test_load_calibrator_missing_artifact_returns_none(tmp_artifacts):
    assert load_calibrator("BTCUSDT", "1m", "20260927-d876e37") is None


def test_load_calibrator_version_mismatch_returns_none(tmp_artifacts):
    _write(tmp_artifacts, "BTCUSDT", "1m",
           {"method": "platt", "a": 1.0, "b": 0.0,
            "model_version": "some-other-version"})
    assert load_calibrator("BTCUSDT", "1m", "20260927-d876e37") is None


def test_load_calibrator_matching_version_returns_artifact(tmp_artifacts):
    artifact = {"method": "platt", "a": 1.0, "b": 0.0,
                "model_version": "20260927-d876e37"}
    _write(tmp_artifacts, "BTCUSDT", "1m", artifact)
    assert load_calibrator("BTCUSDT", "1m", "20260927-d876e37") == artifact


def test_load_calibrator_corrupt_json_returns_none(tmp_artifacts):
    path = calibrator_path("BTCUSDT", "1m")
    path.write_text("{not valid json")
    assert load_calibrator("BTCUSDT", "1m", "any-version") is None


def test_calibrator_path_uses_train_artifacts_dir(tmp_artifacts):
    assert calibrator_path("ethusdt", "1m") == (
        tmp_artifacts / "calibrator_ETHUSDT_1m.json"
    )
