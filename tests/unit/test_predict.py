import numpy as np
import pandas as pd
import pytest

from model import predict as predict_module
from model import train as train_module
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


def test_predict_without_artifact_raises(written):
    with pytest.raises(NoModelArtifact):
        predict("NEVERTRAINED", "1m", bars=_bars())


def test_predict_writes_one_signal_with_version(written):
    metrics = train("TESTCOIN", "1m", bars=_bars())

    signal = predict("TESTCOIN", "1m", bars=_bars())

    assert signal is not None
    assert len(written) == 1
    assert written[0][0]["model_version"] == metrics["model_version"]
    assert written[0][0]["direction"] in ("up", "down")
    assert 0.5 <= written[0][0]["probability"] <= 1.0
    assert written[0][0]["signal_timestamp"] == _bars().index[-1].to_pydatetime()


def test_predict_short_history_returns_none(written):
    train("TESTCOIN", "1m", bars=_bars())

    assert predict("TESTCOIN", "1m", bars=_bars(n=30)) is None
    assert written == []


def test_predict_empty_bars_returns_none(written):
    train("TESTCOIN", "1m", bars=_bars())

    assert predict("TESTCOIN", "1m", bars=pd.DataFrame()) is None
    assert written == []
