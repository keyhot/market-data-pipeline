"""E17 A0 runner: the 2026 cut on DVOL values, the timestamp-only gap
report, the fetch->cache path, and one synthetic trial. DB-free and
network-free (fake getter, tmp cache dir)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import e17_a0_report as rep  # noqa: E402

from ingestion import deribit_dvol as dv  # noqa: E402
from model import implied_vol_band as ivb  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(rep, "DVOL_DIR", tmp_path / "dvol")
    return tmp_path


def _write(cur, ts, close=60.0):
    df = pd.DataFrame({"ts": pd.DatetimeIndex(ts), "open": close, "high": close,
                       "low": close, "close": close})
    dv.save_dvol_csv(df, rep.cache_path(cur))


def test_scoring_loader_never_returns_a_2026_value():
    _write(
        "BTC", pd.date_range("2025-12-31 20:00", "2026-01-01 03:00", freq="h", tz="UTC")
    )
    df = rep.load_dvol_for_scoring("BTC")
    assert df["ts"].max() == pd.Timestamp("2025-12-31 23:00", tz="UTC")


def test_gap_report_returns_counts_only():
    ts = pd.date_range(
        ivb.DVOL_FIRST, ivb.GAP_END, freq="h", tz="UTC", inclusive="left"
    )
    _write("BTC", ts.delete(slice(0, 100)))
    _write("ETH", ts)
    g = rep.gap_report()
    assert set(g["BTC"]) == {"expected", "present", "missing", "share"}
    assert g["BTC"]["missing"] == 100 and g["ETH"]["missing"] == 0


def test_fetch_writes_the_cache_idempotently_with_a_fake_getter():
    rows = [
        [dv.DVOL_START_MS + i * 3_600_000, 1.0, 1.0, 1.0, 50.0 + i] for i in range(5)
    ][::-1]

    def get_json(url, params):
        return {"result": {"data": rows, "continuation": None}}

    a = rep.fetch(("BTC",), get_json=get_json)
    b = rep.fetch(("BTC",), get_json=get_json)
    assert a == b and a["BTC"]["rows"] == 5
    assert set(a["BTC"]) == {"rows", "first", "last"}


def test_seed_trial_returns_contrast_per_kappa_and_the_exchangeable_null():
    out = rep.seed_trial((7, 0.4, (0.0, 1.0), ivb.HALF_LIFE_H, True))
    assert 6400 <= out["n"] <= 6456
    assert np.isfinite(out["control_dq"])
    assert set(out["kappa"]) == {0.0, 1.0}
    assert out["kappa"][1.0]["mean"] > out["kappa"][0.0]["mean"]
    assert set(out["null"]) >= {"lo", "hi", "reject2"}
