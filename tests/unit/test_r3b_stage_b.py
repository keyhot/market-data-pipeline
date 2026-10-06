"""R3-b stage B runner: verdict-window loader, decision rule, run-once guards (synthetic only)."""
import json

import numpy as np
import pandas as pd
import pytest

from scripts import r3b_stage_b as sb


def _candles(start, end):
    idx = pd.date_range(start, end, freq="1min", tz="UTC", inclusive="left")
    return [{"time": int(t.value // 10**6), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0}
            for t in idx]


def _getter(candles):
    def get(frm, to):
        page = [c for c in candles if frm * 1000 <= c["time"] < to * 1000]
        return page[:2000]
    return get


def test_loader_never_returns_a_bar_outside_the_verdict_window():
    c = _candles("2024-12-31 23:50", "2025-01-02 02:00") + _candles("2026-09-27 23:50", "2026-09-28 00:10")
    b = sb.load_verdict("PF_XBTUSD", getter=_getter(c), cache=None)
    assert b.index.min() >= pd.Timestamp("2025-01-01", tz="UTC")
    assert b.index.max() < pd.Timestamp("2026-09-28", tz="UTC")
    assert not ((b.index.day == 1) | ((b.index.day == 2) & (b.index.hour < 1))).any()   # C1 drop
    assert len(b) > 0


def test_verdict_window_constants_match_the_frozen_spec():
    assert sb.VERDICT == ("2025-01-01", "2026-09-28")
    assert sb.K == 8.5 and sb.BOOT == 9999 and sb.METHOD == "t"
    assert sb.SEED_CONTROL == 20261105 and sb.SEED_BOOT == 20261106
    assert sb.MIN_FILLS == pytest.approx(0.5 * 1081)


def _res(lb1, lb2):
    return {"lb_primary": lb1, "lb_paired": lb2}


def test_decide_maps_bounds_gate_and_fill_count():
    ok = dict(stage_a_valid=True, gates_ok=True, n_fills=900)
    assert sb.decide(_res(1.0, 2.0), **ok) == "PASS"
    assert sb.decide(_res(-0.1, 2.0), **ok) == "FAIL"
    assert sb.decide(_res(1.0, -0.1), **ok) == "FAIL"
    assert sb.decide(_res(0.0, 2.0), **ok) == "FAIL"                 # strict > 0
    assert sb.decide(_res(1.0, 2.0), **{**ok, "n_fills": 540}) == "VOID"
    assert sb.decide(_res(1.0, 2.0), **{**ok, "n_fills": 541}) == "PASS"
    assert sb.decide(_res(1.0, 2.0), **{**ok, "gates_ok": False}) == "VOID"
    assert sb.decide(_res(1.0, 2.0), **{**ok, "stage_a_valid": False}) == "VOID"


def test_runner_refuses_without_valid_amended_stage_a(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "OUT", tmp_path / "b.json")
    monkeypatch.setattr(sb, "STAGE_A1", tmp_path / "a1.json")
    with pytest.raises(SystemExit):
        sb.main()                                                   # no stage A1 file
    (tmp_path / "a1.json").write_text(json.dumps({"stage_a1": {"result": "INVALID"}}))
    with pytest.raises(SystemExit):
        sb.main()
    assert not (tmp_path / "b.json").exists()


def test_runner_refuses_to_overwrite_a_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "OUT", tmp_path / "b.json")
    monkeypatch.setattr(sb, "STAGE_A1", tmp_path / "a1.json")
    (tmp_path / "a1.json").write_text(json.dumps({"stage_a1": {"result": "VALID"}}))
    (tmp_path / "b.json").write_text("{}")
    with pytest.raises(SystemExit):
        sb.main()
    assert (tmp_path / "b.json").read_text() == "{}"
