"""E15c report-runner tests. Stage A0 is the only stage actually run by
this handoff; A/B/C are exercised only as guards (refuse without the
prior stage's result, NotImplementedError otherwise)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vol_target_report as vtr  # noqa: E402


def test_stage_a0_never_imports_spot_archive_loader():
    """A0 must read no real returns. Assert via the parsed AST's actual
    import statements (not prose/docstrings, which may legitimately
    mention the loader's name) that the module never imports
    `model.spot_archive` / `load_spot_klines`."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(vtr))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.add(node.module or "")
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
    assert "load_spot_klines" not in imported_names
    assert "model.spot_archive" not in imported_names
    assert "spot_archive" not in imported_names
    assert not hasattr(vtr, "load_spot_klines")


def test_stage_a0_writes_result_with_allowlisted_keys(tmp_path):
    branch, a0 = vtr.run_stage_a0(seeds=range(3), reps=30, results_dir=tmp_path, max_workers=1)
    out_path = tmp_path / "e15c_stage_a0.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert set(payload["statistic"].keys()) == {"a0_power", "a0_size", "a0_mde", "branch"}
    assert payload["verdict"] == branch
    assert branch in ("clean", "contaminated", "VOID_UNDERPOWERED")
    assert payload["inputs"]["data"] == "synthetic only -- no real returns read"


def test_stage_a0_runs_clean_first_skips_contaminated_when_clean_powered(tmp_path, monkeypatch):
    calls = []
    real_run_branch = vtr._run_branch_a0

    def spy(months, seeds, reps, max_workers):
        calls.append(months)
        out = real_run_branch(months, seeds, reps, max_workers)
        if months == vtr.CLEAN_MONTHS:
            out = dict(out, power=0.9)  # force clean to clear the gate
        return out

    monkeypatch.setattr(vtr, "_run_branch_a0", spy)
    branch, a0 = vtr.run_stage_a0(seeds=range(2), reps=20, results_dir=tmp_path, max_workers=1)
    assert branch == "clean"
    assert vtr.CLEAN_MONTHS in calls
    assert vtr.FALLBACK_MONTHS not in calls  # contaminated A0 never ran


def test_stage_a_refuses_without_a0(tmp_path):
    with pytest.raises(RuntimeError, match="stage A0 result missing"):
        vtr.run_stage_a(Path("/nonexistent"), results_dir=tmp_path)


def test_stage_a_refuses_when_a0_void_underpowered(tmp_path):
    (tmp_path / "e15c_stage_a0.json").write_text(json.dumps({"verdict": "VOID_UNDERPOWERED"}))
    with pytest.raises(RuntimeError, match="stage A0 verdict"):
        vtr.run_stage_a(Path("/nonexistent"), results_dir=tmp_path)


def test_stage_a_not_implemented_when_a0_branch_chosen(tmp_path):
    (tmp_path / "e15c_stage_a0.json").write_text(json.dumps({"verdict": "clean"}))
    with pytest.raises(NotImplementedError):
        vtr.run_stage_a(Path("/nonexistent"), results_dir=tmp_path)


def test_stage_b_refuses_without_valid_a(tmp_path):
    (tmp_path / "e15c_stage_a.json").write_text(json.dumps({"verdict": "VOID"}))
    with pytest.raises(RuntimeError, match="not VALID"):
        vtr.run_stage_b(Path("/nonexistent"), results_dir=tmp_path)


def test_stage_c_refuses_without_b_pass_mutant8():
    """Mutant 8: C runs without a B pass. This guard is what kills it,
    once stage C is built; today it's exercised via the missing-file and
    FAIL-verdict refusal paths."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(RuntimeError, match="stage B result missing"):
            vtr.run_stage_c(Path("/nonexistent"), results_dir=Path(d))
        (Path(d) / "e15c_stage_b.json").write_text(json.dumps({"verdict": "FAIL"}))
        with pytest.raises(RuntimeError, match="refusing"):
            vtr.run_stage_c(Path("/nonexistent"), results_dir=Path(d))


def test_choose_branch_gate_is_0_5_not_0_4_via_run_stage_a0(tmp_path, monkeypatch):
    """Mutant 6, exercised end to end: a branch at power 0.45 must NOT be
    chosen as clean."""
    def fake_run_branch(months, seeds, reps, max_workers):
        return {"power": 0.45, "size": 0.05, "mde": 0.01, "median_se": 0.003,
                "median_ebar": 0.7, "median_months_below_1": 10, "n_paths": 2}

    monkeypatch.setattr(vtr, "_run_branch_a0", fake_run_branch)
    branch, a0 = vtr.run_stage_a0(seeds=range(2), reps=20, results_dir=tmp_path, max_workers=1)
    assert branch != "clean"
