"""E15c report-runner tests. Stage A0 and stage A are actually run by this
handoff; B/C are exercised only as guards (refuse without the prior
stage's result, NotImplementedError otherwise)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vol_target_report as vtr  # noqa: E402


def test_stage_a0_is_synthetic_only_even_though_the_module_now_has_a_loader(tmp_path, monkeypatch):
    """A0 itself must read no real returns -- replaces the old AST-based
    'the module never imports the loader' assertion, which broke once
    stage A (which DOES need `load_spot_klines`) was built. Proven
    behaviourally: A0 completes even with the loader wired to explode."""
    def boom(*a, **kw):
        raise AssertionError("stage A0 must never call the real archive loader")

    monkeypatch.setattr(vtr, "load_spot_klines", boom)
    branch, a0 = vtr.run_stage_a0(seeds=range(2), reps=20, results_dir=tmp_path, max_workers=1)
    assert branch in ("clean", "contaminated", "VOID_UNDERPOWERED")


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


def _fake_a0_result(branch: str, size_gate_ok: bool = True) -> dict:
    return {
        "verdict": branch,
        "statistic": {"a0_power": 0.62, "a0_size": 0.0, "a0_mde": 0.0078, "branch": branch},
        "extra": {"size_gate_ok_on_chosen_branch": size_gate_ok},
    }


def test_stage_a_refuses_when_a0_size_gate_failed_on_chosen_branch(tmp_path):
    (tmp_path / "e15c_stage_a0.json").write_text(
        json.dumps(_fake_a0_result("contaminated", size_gate_ok=False))
    )
    with pytest.raises(RuntimeError, match="size gate"):
        vtr.run_stage_a(Path("/nonexistent"), results_dir=tmp_path)


# --------------------------------------------------------------------------
# Stage A -- pure helper checks (small, hand-constructed fixtures)
# --------------------------------------------------------------------------

def _synthetic_close(seed: int, warmup_months: int, scored_months: int,
                      sigmas=None, start="2020-01-01") -> pd.Series:
    """Warm-up + scored daily close. `sigmas`, if given, is a list of one
    sigma per scored month (so vol actually varies and exposure moves);
    otherwise a single constant sigma=0.02 is used throughout."""
    rng = np.random.default_rng(seed)
    months_n = warmup_months + scored_months
    start_ts = pd.Timestamp(start, tz="UTC") - pd.DateOffset(months=warmup_months) - pd.Timedelta(days=1)
    month_starts = pd.period_range(start_ts + pd.Timedelta(days=1), periods=months_n, freq="M")
    all_rets = []
    for i, m in enumerate(month_starts):
        if sigmas is None or i < warmup_months:
            sigma = 0.02
        else:
            sigma = sigmas[i - warmup_months]
        all_rets.append(rng.normal(0, sigma, m.days_in_month))
    rc = np.concatenate(all_rets)
    close = pd.Series(
        np.exp(np.cumsum(np.concatenate([[0.0], rc]))) * 100.0,
        index=pd.date_range(start_ts, periods=len(rc) + 1, freq="D", tz="UTC"),
    )
    return close


@pytest.fixture()
def small_stage_a(monkeypatch, tmp_path):
    """A fast stage-A run on a tiny synthetic 6-month contaminated-branch
    window, with A0 pre-faked as already-chosen 'contaminated'."""
    (tmp_path / "e15c_stage_a0.json").write_text(json.dumps(_fake_a0_result("contaminated")))

    monkeypatch.setattr(vtr, "VALIDATION_A_FIRST", "2020-01")
    monkeypatch.setattr(vtr, "VALIDATION_A_LAST", "2020-06")
    monkeypatch.setattr(vtr, "CONTAM_B_FIRST", "2020-01")
    monkeypatch.setattr(vtr, "CONTAM_B_LAST", "2020-06")
    monkeypatch.setattr(vtr, "A4_MONTHS_FALLBACK", ("2020-01", "2020-03", "2020-06"))

    # Vol swings month to month so e actually moves for A6.
    sigmas = [0.01, 0.06, 0.01, 0.06, 0.01, 0.06]
    closes = {
        "BTCUSDT": _synthetic_close(1, warmup_months=8, scored_months=6, sigmas=sigmas),
        "ETHUSDT": _synthetic_close(2, warmup_months=8, scored_months=6, sigmas=sigmas),
    }

    def fake_load_spot_klines(root, symbol, interval):
        close = closes[symbol]
        frame = pd.DataFrame({
            "open": close, "high": close, "low": close, "close": close, "volume": 0.0,
        })
        return frame

    monkeypatch.setattr(vtr, "load_spot_klines", fake_load_spot_klines)
    return tmp_path


def test_stage_a_keys_subset_of_allowlist(small_stage_a):
    verdict, failing, statistic = vtr.run_stage_a(Path("."), results_dir=small_stage_a)
    assert set(statistic.keys()) <= vtr.STAGE_A_ALLOWLIST
    written = json.loads((small_stage_a / "e15c_stage_a.json").read_text())
    assert set(written["statistic"].keys()) <= vtr.STAGE_A_ALLOWLIST
    assert verdict in ("VALID", "VOID")


def test_stage_a_json_has_expected_top_level_keys(small_stage_a):
    vtr.run_stage_a(Path("."), results_dir=small_stage_a)
    written = json.loads((small_stage_a / "e15c_stage_a.json").read_text())
    for key in ("experiment", "git_hash", "timestamp", "inputs", "parameters",
                "statistic", "ci", "verdict", "extra"):
        assert key in written
    assert written["experiment"] == "e15c_stage_a"
    for stat_key in ("a1_ci_lo", "a2_ok", "a3_violations", "a4_mismatches",
                     "a5_valid_months", "a6_ebar", "a6_months_below"):
        assert set(written["statistic"][stat_key].keys()) == set(vtr.SYMBOLS)


def test_stage_a_statistic_has_no_stage_b_leakage(small_stage_a):
    """Neither `statistic` nor `extra` may contain a stage-B primary-metric
    key (BH/ebar-book ES comparisons on the B window)."""
    vtr.run_stage_a(Path("."), results_dir=small_stage_a)
    written = json.loads((small_stage_a / "e15c_stage_a.json").read_text())
    forbidden = ("delta_es_bh", "delta_es_e", "es_bh", "es_vt", "ci_bh", "ci_ebar", "winkler")
    for block_name in ("statistic", "extra"):
        for key in written[block_name].keys():
            low = key.lower()
            assert not any(f in low for f in forbidden), f"{block_name}.{key}"


def test_stage_a_never_calls_score_pair_or_scores_b():
    """Stage A's own helper functions must never call `score_pair` (the
    stage-B pipeline, which computes the ΔES_BH / ΔES_ebar primary
    metric) -- checked at the source level so it can't sneak back in."""
    import inspect
    for fn in (vtr.run_stage_a, vtr._a1_oracle, vtr._a2_identity,
               vtr._a3_compounding, vtr._a4_truncation, vtr._a5_validity,
               vtr._a6_moves, vtr.stage_a_checks):
        src = inspect.getsource(fn)
        assert "score_pair" not in src, fn.__name__


def test_stage_a_bootstrap_called_exactly_once_per_symbol_from_a1(small_stage_a, monkeypatch):
    calls = []
    real_bootstrap = vtr.paired_block_bootstrap

    def spy(*args, **kwargs):
        calls.append(1)
        return real_bootstrap(*args, **kwargs)

    monkeypatch.setattr(vtr, "paired_block_bootstrap", spy)
    vtr.run_stage_a(Path("."), results_dir=small_stage_a)
    assert len(calls) == len(vtr.SYMBOLS)


def test_load_close_truncates_bars_opening_on_or_after_2026(monkeypatch):
    idx = pd.date_range("2025-12-29", "2026-01-02", freq="D", tz="UTC")
    close = pd.Series(np.linspace(100, 101, len(idx)), index=idx)

    def fake_loader(root, symbol, interval):
        return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 0.0})

    monkeypatch.setattr(vtr, "load_spot_klines", fake_loader)
    result = vtr.load_close(Path("."), "BTCUSDT")
    assert result.index.max() < vtr.CUTOFF_2026
    assert result.index.max() == pd.Timestamp("2025-12-31", tz="UTC")


# --------------------------------------------------------------------------
# stage_a_checks -- boundary mutants (analogous to E15b's boundary test)
# --------------------------------------------------------------------------

def test_stage_a_checks_boundaries():
    a2_ok = {"validation_a": True, "chosen_b": True}
    a3_viol = {"validation_a": 0, "chosen_b": 0}
    a6_pass = {"real": {"ok": True}, "oracle": {"ok": True}}

    # a1: strict > 0
    assert vtr.stage_a_checks(0.0, a2_ok, a3_viol, 0, 72, 72, a6_pass)["a1"] is False
    assert vtr.stage_a_checks(1e-9, a2_ok, a3_viol, 0, 72, 72, a6_pass)["a1"] is True

    # a2 per-window
    assert vtr.stage_a_checks(1.0, {"validation_a": False, "chosen_b": True},
                               a3_viol, 0, 72, 72, a6_pass)["a2_validation_a"] is False
    assert vtr.stage_a_checks(1.0, {"validation_a": True, "chosen_b": False},
                               a3_viol, 0, 72, 72, a6_pass)["a2_chosen_b"] is False

    # a3 per-window
    assert vtr.stage_a_checks(1.0, a2_ok, {"validation_a": 1, "chosen_b": 0},
                               0, 72, 72, a6_pass)["a3_validation_a"] is False
    assert vtr.stage_a_checks(1.0, a2_ok, {"validation_a": 0, "chosen_b": 1},
                               0, 72, 72, a6_pass)["a3_chosen_b"] is False

    # a4
    assert vtr.stage_a_checks(1.0, a2_ok, a3_viol, 1, 72, 72, a6_pass)["a4"] is False

    # a5 must compare against the WINDOW'S expected count, not a literal 72
    assert vtr.stage_a_checks(1.0, a2_ok, a3_viol, 0, 71, 72, a6_pass)["a5"] is False
    assert vtr.stage_a_checks(1.0, a2_ok, a3_viol, 0, 27, 27, a6_pass)["a5"] is True

    # a6: both real AND oracle must be ok
    assert vtr.stage_a_checks(1.0, a2_ok, a3_viol, 0, 72, 72,
                               {"real": {"ok": True}, "oracle": {"ok": False}})["a6"] is False
    assert vtr.stage_a_checks(1.0, a2_ok, a3_viol, 0, 72, 72,
                               {"real": {"ok": False}, "oracle": {"ok": True}})["a6"] is False

    assert vtr.stage_a_checks(1.0, a2_ok, a3_viol, 0, 72, 72, a6_pass) == {
        "a1": True, "a2_validation_a": True, "a2_chosen_b": True,
        "a3_validation_a": True, "a3_chosen_b": True, "a4": True, "a5": True, "a6": True,
    }


def test_a3_tolerance_1e_9_catches_a_1e_7_perturbation():
    """Pins A3_TOL at 1e-9 (pre-reg: 'within 1e-9'): a ~1e-7 discrepancy in
    one day's y must count as a violation at the declared tolerance, and
    must NOT at a loosened 1e-6."""
    idx = pd.date_range("2020-01-01", periods=32, freq="D", tz="UTC")
    close = pd.Series(np.linspace(100.0, 101.0, len(idx)), index=idx)
    e = pd.Series([0.7], index=pd.period_range("2020-02", periods=1, freq="M"))
    vm = vtr.managed_daily(close, e, cost_side=0.0011, e_start=1.0)
    assert vtr.A3_TOL == pytest.approx(1e-9)
    assert vtr._a3_compounding(close, e, vm, tol=vtr.A3_TOL) == 0

    perturbed = vm.copy()
    feb_idx = perturbed.index[pd.PeriodIndex(perturbed.index, freq="M") == pd.Period("2020-02", freq="M")]
    perturbed.loc[feb_idx[-1], "y"] += 1e-7
    assert vtr._a3_compounding(close, e, perturbed, tol=1e-9) == 1
    assert vtr._a3_compounding(close, e, perturbed, tol=1e-6) == 0


def test_a6_ebar_boundary_0_90_passes_le_not_lt():
    """No existing model-level test sits exactly on ebar == 0.90 -- pins
    the '<=' (not '<') reading so a mutant there is killable via A6."""
    e = pd.Series([0.8, 1.0])  # mean == 0.9 exactly, n_below(<0.95) == 1 of 2
    ebar, n_below, ok = vtr.moves_exposure(e)
    assert ebar == pytest.approx(0.90)
    assert n_below == 1
    assert ok is True


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
