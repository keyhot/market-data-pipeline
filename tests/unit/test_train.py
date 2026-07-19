import numpy as np
import pandas as pd
import pytest

from model import train as train_module
from model.train import artifact_path, latest_artifact, model_version, train


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


def test_train_writes_artifact_and_metrics(tmp_artifacts):
    metrics = train("TESTCOIN", "1m", bars=_bars())

    assert 0.0 <= metrics["holdout_accuracy"] <= 1.0
    assert metrics["rows_train"] > metrics["rows_holdout"]
    path = artifact_path("TESTCOIN", "1m", metrics["model_version"])
    assert path.exists()
    assert path.with_suffix(".metrics.json").exists()
    assert latest_artifact("TESTCOIN", "1m") == path


def test_train_is_deterministic():
    m1 = train("TESTCOIN", "1m", bars=_bars())
    m2 = train("TESTCOIN", "1m", bars=_bars())

    assert m1["holdout_accuracy"] == m2["holdout_accuracy"]
    assert m1["holdout_logloss"] == m2["holdout_logloss"]


def test_train_rejects_insufficient_data():
    with pytest.raises(ValueError, match="not enough"):
        train("TESTCOIN", "1m", bars=_bars(n=150))


def test_train_rejects_empty_bars():
    with pytest.raises(ValueError, match="no stored bars"):
        train("TESTCOIN", "1m", bars=pd.DataFrame())


def test_model_version_format():
    version = model_version()
    date_part, sha_part = version.split("-", 1)
    assert len(date_part) == 8 and date_part.isdigit()
    assert sha_part


def test_latest_artifact_none_when_untrained():
    assert latest_artifact("NEVERTRAINED", "1m") is None
