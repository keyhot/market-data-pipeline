"""KI-097: every report script's verdict must persist as JSON, not just
print. Tests the shared helper (`scripts/report_results.write_result`) with
a tiny fixture — never re-runs a real experiment."""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from report_results import write_result  # noqa: E402


def test_write_result_persists_json(tmp_path):
    path = write_result(
        "demo_BTCUSDT",
        inputs={"symbol": "BTCUSDT", "rows": 10},
        parameters={"bootstrap": 1000},
        statistic={"delta_auc": 0.01},
        ci={"low": -0.01, "high": 0.03},
        verdict="FAIL",
        results_dir=tmp_path,
    )
    assert path == tmp_path / "demo_BTCUSDT.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["experiment"] == "demo_BTCUSDT"
    assert data["verdict"] == "FAIL"
    assert data["inputs"] == {"symbol": "BTCUSDT", "rows": 10}
    assert data["ci"] == {"low": -0.01, "high": 0.03}
    assert "git_hash" in data and data["git_hash"]
    assert "timestamp" in data and data["timestamp"]


def test_write_result_creates_missing_directory(tmp_path):
    out_dir = tmp_path / "nested" / "results"
    assert not out_dir.exists()
    path = write_result(
        "demo", inputs={}, parameters={}, statistic={}, ci=None,
        verdict="PASS", results_dir=out_dir,
    )
    assert path.exists()


def test_write_result_serializes_numpy_scalars(tmp_path):
    """The trap KI-097 exists to catch: auc()/np.percentile/np.mean return
    numpy scalars, and plain json.dumps raises TypeError on them after the
    walk-forward has already run."""
    path = write_result(
        "demo_numpy",
        inputs={"rows": np.int64(140_250)},
        parameters={"seed": np.int64(7)},
        statistic={"delta_auc": np.float64(0.0123), "positive_years": np.int64(3)},
        ci=[np.float64(-0.01), np.float64(0.03)],
        verdict="PASS",
        extra={"control_ok": np.bool_(True), "draws": np.array([1.0, 2.0])},
        results_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["inputs"]["rows"] == 140_250
    assert data["statistic"]["delta_auc"] == 0.0123
    assert data["extra"]["control_ok"] is True
    assert data["extra"]["draws"] == [1.0, 2.0]


def test_write_result_default_dir_is_scripts_results(tmp_path, monkeypatch):
    """Uses the module-level RESULTS_DIR when no override is given — read at
    call time so tests can monkeypatch it, never writing into the real
    scripts/results/ directory from the suite."""
    import report_results
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path / "results")
    path = write_result(
        "demo", inputs={}, parameters={}, statistic={}, ci=None, verdict="VOID",
    )
    assert path == tmp_path / "results" / "demo.json"
    assert path.exists()
