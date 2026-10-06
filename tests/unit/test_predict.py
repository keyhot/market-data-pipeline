import json

import numpy as np
import pandas as pd
import pytest

from model import predict as predict_module
from model import train as train_module
from model.calibration import calibrator_path
from model.predict import NoModelArtifact, predict
from model.train import train


def _bars(n=1500, seed=11):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-06-01", periods=n, freq="1min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.001,
            "Low": close * 0.999,
            "Close": close,
            "Volume": rng.integers(100, 10_000, n).astype(float),
        },
        index=index,
    )


@pytest.fixture(autouse=True)
def tmp_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(train_module, "ARTIFACTS_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def written(monkeypatch):
    calls = []
    monkeypatch.setattr(predict_module, "write_signals", calls.append)
    return calls


@pytest.fixture(autouse=True)
def no_open_call(monkeypatch):
    """No previous signal unless a test says otherwise (get_signals is a DB read)."""
    previous = []
    monkeypatch.setattr(predict_module, "get_signals",
                        lambda symbol, interval, limit: previous[:limit])
    return previous


def _bars_15m(n=1500, seed=5):
    bars = _bars(n=n, seed=seed)
    bars.index = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return bars


def _train_live(monkeypatch, quantile=0.0):
    """The live model: 15m bars, one bar ahead. quantile 0 = gate open."""
    monkeypatch.setattr(train_module, "GATE_QUANTILE", quantile)
    return train("TESTCOIN", "15m", horizon_bars=1, bars=_bars_15m())


def test_predict_without_artifact_raises(written):
    with pytest.raises(NoModelArtifact):
        predict("NEVERTRAINED", "1m", bars=_bars())


def test_a_1m_model_no_longer_serves_the_1m_stream(written):
    # The 21-hour 1m artifact is retired: only a 15m model may answer.
    train("TESTCOIN", "1m", bars=_bars())
    with pytest.raises(NoModelArtifact):
        predict("TESTCOIN", "1m", bars=_bars())


def test_the_15m_model_answers_the_1m_stream_as_of_the_newest_minute(
        written, monkeypatch):
    metrics = _train_live(monkeypatch)
    minutes = _bars()

    signal = predict("TESTCOIN", "1m", bars=minutes)

    assert signal is not None and "held" not in signal
    assert len(written) == 1
    row = written[0][0]
    assert row["model_version"] == metrics["model_version"]
    # Unchanged on air: a 1m signal, 15 bars (minutes) ahead, stamped now.
    assert row["interval"] == "1m"
    assert row["horizon_bars"] == 15
    assert row["signal_timestamp"] == minutes.index[-1].to_pydatetime()
    assert row["direction"] in ("up", "down")
    assert 0.5 <= row["probability"] <= 1.0


def test_a_coin_flip_is_held_not_published(written, monkeypatch):
    _train_live(monkeypatch, quantile=0.8)
    cutoff = train_module.latest_metrics("TESTCOIN", "15m")["confidence_cutoff"]
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: 0.5 + cutoff / 2)

    assert predict("TESTCOIN", "1m", bars=_bars()) == {
        "held": "low_confidence"}
    assert written == []


def test_a_confident_call_clears_the_gate(written, monkeypatch):
    _train_live(monkeypatch, quantile=0.8)
    cutoff = train_module.latest_metrics("TESTCOIN", "15m")["confidence_cutoff"]
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: 0.5 - cutoff * 1.01)

    signal = predict("TESTCOIN", "1m", bars=_bars())

    assert signal["direction"] == "down"
    assert len(written) == 1


def test_no_new_call_while_the_last_one_is_open(written, monkeypatch,
                                                no_open_call):
    _train_live(monkeypatch)
    minutes = _bars()
    now = minutes.index[-1]
    no_open_call.append({"signal_timestamp": (now - pd.Timedelta(minutes=14))
                         .isoformat(), "horizon_bars": 15})

    assert predict("TESTCOIN", "1m", bars=minutes) == {"held": "call_open"}
    assert written == []


def test_a_new_call_once_the_last_one_has_resolved(written, monkeypatch,
                                                   no_open_call):
    _train_live(monkeypatch)
    minutes = _bars()
    now = minutes.index[-1]
    no_open_call.append({"signal_timestamp": (now - pd.Timedelta(minutes=15))
                         .isoformat(), "horizon_bars": 15})

    assert "held" not in predict("TESTCOIN", "1m", bars=minutes)
    assert len(written) == 1


def test_training_records_the_cutoff_as_a_holdout_quantile(monkeypatch):
    metrics = _train_live(monkeypatch, quantile=0.8)
    confidence = metrics["holdout_confidence_quantiles"]
    assert metrics["confidence_cutoff"] == pytest.approx(confidence["0.8"])
    assert 0.0 < metrics["confidence_cutoff"] < 0.5
    assert metrics["gate_quantile"] == 0.8


def test_predict_short_history_returns_none(written, monkeypatch):
    _train_live(monkeypatch)

    assert predict("TESTCOIN", "1m", bars=_bars(n=30)) is None
    assert written == []


def test_predict_empty_bars_returns_none(written, monkeypatch):
    _train_live(monkeypatch)

    assert predict("TESTCOIN", "1m", bars=pd.DataFrame()) is None
    assert written == []


def test_a_matching_calibrator_rescales_the_published_probability(
        written, monkeypatch):
    metrics = _train_live(monkeypatch, quantile=0.8)
    cutoff = metrics["confidence_cutoff"]
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: 0.5 + max(cutoff * 1.5, 0.3))
    path = calibrator_path("TESTCOIN", "1m")
    path.write_text(json.dumps({
        "method": "platt", "a": 0.1, "b": 0.0,
        "model_version": metrics["model_version"],
    }))

    signal = predict("TESTCOIN", "1m", bars=_bars())

    raw_p = 0.5 + max(cutoff * 1.5, 0.3)
    assert signal["direction"] == "up"
    # Shallow slope (a=0.1) pulls a confident raw p back toward 0.5 — the
    # calibrated call is honest about being less sure than the raw model.
    assert 0.5 < signal["probability"] < raw_p


def test_a_matching_calibrator_rescales_a_down_call_too(written, monkeypatch):
    """Direction is decided on raw p (down: raw_p < 0.5), but the published
    probability must calibrate P(down) = 1 - calibrated P(up), not the raw
    P(up) run through the calibrator directly — pinning this against a
    mutation that drops the `if direction == "up" else 1 - calibrated_up`
    branch and always publishes `calibrated_up`."""
    metrics = _train_live(monkeypatch, quantile=0.8)
    cutoff = metrics["confidence_cutoff"]
    raw_p_up = 0.5 - max(cutoff * 1.5, 0.3)  # < 0.5 -> direction "down"
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: raw_p_up)
    path = calibrator_path("TESTCOIN", "1m")
    path.write_text(json.dumps({
        "method": "platt", "a": 0.1, "b": 0.0,
        "model_version": metrics["model_version"],
    }))

    signal = predict("TESTCOIN", "1m", bars=_bars())

    from model.calibration import apply_calibrator
    calibrated_up = apply_calibrator(
        raw_p_up, {"method": "platt", "a": 0.1, "b": 0.0})
    assert signal["direction"] == "down"
    assert signal["probability"] == pytest.approx(1 - calibrated_up)


def test_a_version_mismatched_calibrator_is_ignored(written, monkeypatch):
    metrics = _train_live(monkeypatch, quantile=0.8)
    cutoff = metrics["confidence_cutoff"]
    raw_p = 0.5 + max(cutoff * 1.5, 0.3)
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: raw_p)
    path = calibrator_path("TESTCOIN", "1m")
    path.write_text(json.dumps({
        "method": "platt", "a": 0.1, "b": 0.0,
        "model_version": "not-the-live-version",
    }))

    signal = predict("TESTCOIN", "1m", bars=_bars())

    # Fail-open: a calibrator for a different model_version is never applied.
    assert signal["probability"] == pytest.approx(raw_p)


def test_no_calibrator_artifact_falls_back_to_raw_p(written, monkeypatch):
    metrics = _train_live(monkeypatch, quantile=0.8)
    cutoff = metrics["confidence_cutoff"]
    raw_p = 0.5 + max(cutoff * 1.5, 0.3)
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: raw_p)

    signal = predict("TESTCOIN", "1m", bars=_bars())

    assert signal["probability"] == pytest.approx(raw_p)


def test_the_model_reads_fifteen_minute_bars_rolled_to_now(written, monkeypatch):
    from model.features import build_latest_features, roll_bars

    _train_live(monkeypatch)
    minutes = _bars()
    seen = []
    monkeypatch.setattr(predict_module, "_probability_up",
                        lambda booster, features: seen.append(features) or 0.9)

    predict("TESTCOIN", "1m", bars=minutes)

    expected = build_latest_features(roll_bars(minutes, 15))
    pd.testing.assert_frame_equal(seen[0], expected)
