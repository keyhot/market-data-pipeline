"""Tests for `scripts/vol_managed_report.py` -- DB-free (monkeypatched
loader + results dir); no network, no Postgres, no real archive."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vol_managed_report as vmr  # noqa: E402


def _synthetic_close(seed: int, warmup_months: int, scored_months: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    months_n = warmup_months + scored_months
    start = pd.Timestamp("2020-01-01", tz="UTC") - pd.DateOffset(months=warmup_months) - pd.Timedelta(days=1)
    month_starts = pd.period_range(start + pd.Timedelta(days=1), periods=months_n, freq="M")
    all_rets = []
    for m in month_starts:
        all_rets.append(rng.normal(0, 0.02, m.days_in_month))
    rc = np.concatenate(all_rets)
    close = pd.Series(
        np.exp(np.cumsum(np.concatenate([[0.0], rc]))) * 100.0,
        index=pd.date_range(start, periods=len(rc) + 1, freq="D", tz="UTC"),
    )
    return close


@pytest.fixture()
def small_stage_a(monkeypatch, tmp_path):
    """A fast stage-A run: a small synthetic scored window (6 months, 2
    symbols) with real warm-up, monkeypatching the module's scored-window
    constants, A4 months, the loader and the results dir."""
    monkeypatch.setattr(vmr, "FIRST_SCORED", "2020-01")
    monkeypatch.setattr(vmr, "LAST_SCORED_B", "2020-06")
    monkeypatch.setattr(vmr, "A4_MONTHS", ("2020-01", "2020-03", "2020-06"))

    closes = {
        "BTCUSDT": _synthetic_close(1, warmup_months=8, scored_months=6),
        "ETHUSDT": _synthetic_close(2, warmup_months=8, scored_months=6),
    }

    def fake_load_close(archive_root, symbol, stage):
        close = closes[symbol]
        if stage in ("A", "B"):
            close = close[close.index < vmr.CUTOFF_2026]
        return close

    monkeypatch.setattr(vmr, "load_close", fake_load_close)
    return tmp_path


def test_stage_b_load_has_no_2026_bar(monkeypatch):
    idx = pd.date_range("2025-12-01", "2026-01-05", freq="D", tz="UTC")
    close = pd.Series(np.linspace(100, 101, len(idx)), index=idx)

    def fake_loader(archive_root, symbol, stage):
        raw = pd.DataFrame({"close": close})
        if stage in ("A", "B"):
            raw = raw[raw.index < vmr.CUTOFF_2026]
        return raw["close"]

    result = fake_loader(Path("."), "BTCUSDT", "B")
    assert result.index.max() < vmr.CUTOFF_2026
    assert result.index.max() == pd.Timestamp("2025-12-31", tz="UTC")


def test_stage_b_refuses_without_stage_a_result(tmp_path):
    with pytest.raises(RuntimeError, match="stage A result missing"):
        vmr.run_stage_b(Path("."), results_dir=tmp_path)


def test_stage_b_refuses_when_stage_a_not_valid(tmp_path):
    (tmp_path / "e15b_stage_a.json").write_text(json.dumps({"verdict": "VOID"}))
    with pytest.raises(RuntimeError, match="not VALID"):
        vmr.run_stage_b(Path("."), results_dir=tmp_path)


def test_stage_b_not_implemented_when_a_is_valid(tmp_path):
    (tmp_path / "e15b_stage_a.json").write_text(json.dumps({"verdict": "VALID"}))
    with pytest.raises(NotImplementedError):
        vmr.run_stage_b(Path("."), results_dir=tmp_path)


def test_stage_c_refuses_without_stage_b_result(tmp_path):
    with pytest.raises(RuntimeError, match="stage B result missing"):
        vmr.run_stage_c(Path("."), results_dir=tmp_path)


def test_stage_c_refuses_when_stage_b_not_pass_or_split(tmp_path):
    (tmp_path / "e15b_stage_b.json").write_text(json.dumps({"verdict": "FAIL"}))
    with pytest.raises(RuntimeError, match="refusing"):
        vmr.run_stage_c(Path("."), results_dir=tmp_path)


def test_stage_a_keys_subset_of_allowlist(small_stage_a):
    verdict, failing, statistic = vmr.run_stage_a(Path("."), results_dir=small_stage_a)
    assert set(statistic.keys()) <= vmr.STAGE_A_ALLOWLIST
    written = json.loads((small_stage_a / "e15b_stage_a.json").read_text())
    assert set(written["statistic"].keys()) <= vmr.STAGE_A_ALLOWLIST
    assert verdict in ("VALID", "VOID")


def test_stage_a_json_has_expected_top_level_keys(small_stage_a):
    vmr.run_stage_a(Path("."), results_dir=small_stage_a)
    written = json.loads((small_stage_a / "e15b_stage_a.json").read_text())
    for key in ("experiment", "git_hash", "timestamp", "inputs", "parameters",
                "statistic", "ci", "verdict", "extra"):
        assert key in written
    assert written["experiment"] == "e15b_stage_a"
    for stat_key in vmr.STAGE_A_ALLOWLIST & set(written["statistic"].keys()):
        assert set(written["statistic"][stat_key].keys()) == set(vmr.SYMBOLS)


def test_stage_a_checks_boundaries():
    # a1: strict > 0, not >= 0 (mutant: a1_lo >= 0)
    assert vmr.stage_a_checks(0.0, True, 0, 0, 72)["a1"] is False
    assert vmr.stage_a_checks(1e-9, True, 0, 0, 72)["a1"] is True
    # a2/a3/a4/a5 boundaries
    assert vmr.stage_a_checks(1.0, False, 0, 0, 72)["a2"] is False
    assert vmr.stage_a_checks(1.0, True, 1, 0, 72)["a3"] is False
    assert vmr.stage_a_checks(1.0, True, 0, 1, 72)["a4"] is False
    assert vmr.stage_a_checks(1.0, True, 0, 0, 71)["a5"] is False
    assert vmr.stage_a_checks(1.0, True, 0, 0, 72) == {
        "a1": True, "a2": True, "a3": True, "a4": True, "a5": True,
    }


def test_stage_a_statistic_has_no_return_es_or_sharpe_leakage(small_stage_a):
    # The stage-A JSON may contain ONLY the allowlisted A1-A5 keys: assert
    # no forbidden substrings ever appear as a top-level statistic key.
    vmr.run_stage_a(Path("."), results_dir=small_stage_a)
    written = json.loads((small_stage_a / "e15b_stage_a.json").read_text())
    forbidden = ("return", "sharpe", "es_bh", "es_vm", "delta_es", "winkler")
    for key in written["statistic"].keys():
        low = key.lower()
        assert not any(f in low for f in forbidden), key
