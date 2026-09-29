"""KI-100: `scripts/cross_section_report.py`'s verdict code had no test
that could fail -- no test imported the module at all. Covers the pass
rule (`pass_rule`, extracted verbatim from `score()` so its `c1`/`c2`
booleans are testable in isolation), `_null`, `_stats` and `_last_beta`.
Fixtures are hand-computable or comparison-based (poison a row the code is
supposed to ignore and assert the output doesn't move) so an `and`->`or`,
a dropped cost term, a broken date filter, or a wrong rounding/formula all
fail loudly. Does not touch `model/cross_section.py`."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import cross_section_report as csr  # noqa: E402
import model.cross_section as mcs  # noqa: E402


# ------------------------------------------------------------------ pass_rule

def test_pass_rule_passes_when_both_clauses_hold():
    null = np.arange(100, dtype=float)  # p97.5 == 97.525
    out = csr.pass_rule(net_ci_low_bps=1.0, gf=98.0, null=null)
    assert out["1_net_ci_above_0"] is True
    assert out["2_beats_null_p97.5"] is True
    assert out["verdict"] == "PASS"


def test_pass_rule_falsified_when_ci_not_above_zero_despite_beating_null():
    """An `and`->`or` mutant would PASS this: c2 alone is true."""
    null = np.arange(100, dtype=float)
    out = csr.pass_rule(net_ci_low_bps=0.0, gf=98.0, null=null)  # 0 is not > 0
    assert out["1_net_ci_above_0"] is False
    assert out["2_beats_null_p97.5"] is True
    assert out["verdict"] == "FALSIFIED"


def test_pass_rule_falsified_when_null_not_beaten_despite_ci_above_zero():
    """An `and`->`or` mutant would PASS this too: c1 alone is true."""
    null = np.arange(100, dtype=float)
    out = csr.pass_rule(net_ci_low_bps=1.0, gf=50.0, null=null)  # below p97.5
    assert out["1_net_ci_above_0"] is True
    assert out["2_beats_null_p97.5"] is False
    assert out["verdict"] == "FALSIFIED"


def test_pass_rule_c2_boundary_is_strict_greater_than():
    """gf exactly at the null's p97.5 must NOT pass: `>` not `>=`."""
    null = np.arange(100, dtype=float)
    p975 = float(np.quantile(null, 0.975))
    out = csr.pass_rule(net_ci_low_bps=1.0, gf=p975, null=null)
    assert out["2_beats_null_p97.5"] is False


# ----------------------------------------------------------------------- _stats

def _pnl(net, gross, funding, cost, turnover) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(net), freq="D", tz="UTC")
    return pd.DataFrame({"net": net, "gross": gross, "funding": funding,
                         "cost": cost, "turnover": turnover}, index=idx)


def test_stats_zero_series_zero_variance_sharpe():
    """All-zero net -> std() is exactly 0.0 (no float rounding possible for
    identical zeros), so `sd > 0` must take the explicit `else 0.0` branch.
    A bare `mean/sd` with no guard would raise ZeroDivisionError or hand
    back nan/inf instead."""
    pnl = _pnl(net=[0.0] * 10, gross=[0.0015] * 10, funding=[-0.0001] * 10,
              cost=[0.0004] * 10, turnover=[0.4] * 10)
    out = csr._stats(pnl)
    assert out["net_bps"] == [0.0, 0.0, 0.0]
    assert out["gross_bps"] == 15.0
    assert out["funding_bps"] == -1.0
    assert out["cost_bps"] == 4.0
    assert out["turnover"] == 0.4
    assert out["sharpe_ann"] == 0.0


def test_stats_nonconstant_series_sharpe_formula():
    """block_bootstrap_ci's default block=10 with exactly 10 rows has only
    ONE possible resample start, so the CI collapses to the sample mean
    deterministically -- letting the sharpe formula (ddof=1, sqrt(365)) be
    pinned by hand without fighting bootstrap randomness."""
    net = [0.0] * 5 + [0.002] * 5
    pnl = _pnl(net=net, gross=[0.001] * 10, funding=[0.0] * 10,
              cost=[0.0] * 10, turnover=[1.0] * 10)
    out = csr._stats(pnl)
    assert out["net_bps"] == [10.0, 10.0, 10.0]
    expected_sharpe = round(np.mean(net) / np.std(net, ddof=1) * np.sqrt(365), 2)
    assert out["sharpe_ann"] == pytest.approx(expected_sharpe)
    assert out["sharpe_ann"] == pytest.approx(18.12)


# ------------------------------------------------------------------------ _null

def _null_fixture():
    idx = pd.DatetimeIndex(
        ["2022-12-30", "2022-12-31", "2023-01-01", "2023-01-02"], tz="UTC")
    cols = ["A", "B"]
    w = pd.DataFrame([[0.5, -0.5]] * 4, index=idx, columns=cols)
    elig = pd.DataFrame(True, index=idx, columns=cols)
    fwd = pd.DataFrame([[100.0, 100.0], [100.0, 100.0],
                       [0.01, -0.01], [0.02, -0.02]], index=idx, columns=cols)
    ffund = pd.DataFrame([[100.0, 100.0], [100.0, 100.0],
                         [0.001, 0.001], [0.002, 0.002]], index=idx, columns=cols)
    return w, elig, fwd, ffund


def test_null_ignores_rows_outside_test_window():
    """The first two days are BEFORE `csr.TEST_START` (2023-01-01) and are
    poisoned with huge returns. `_null` must slice `.loc[TEST_START:
    TEST_END]` before averaging -- a broken/dropped filter would let the
    poison leak in and the result would blow up."""
    w, elig, fwd, ffund = _null_fixture()
    baseline = csr._null(w, elig, fwd, ffund, 0.0005, 0.0, seeds=3)
    poisoned = fwd.copy()
    poisoned.loc[poisoned.index[0]] = 99_999.0
    poisoned_out = csr._null(w, elig, poisoned, ffund, 0.0005, 0.0, seeds=3)
    assert np.allclose(baseline, poisoned_out)


def test_null_uses_gross_plus_funding_not_net():
    """`_null` averages `gross + funding`, never `net` (which also
    subtracts cost). A wildly different fee must not move the result --
    a mutant that summed `net` instead would make it move a lot."""
    w, elig, fwd, ffund = _null_fixture()
    cheap = csr._null(w, elig, fwd, ffund, 0.0005, 0.0, seeds=3)
    expensive = csr._null(w, elig, fwd, ffund, 999.0, 0.0, seeds=3)
    assert np.allclose(cheap, expensive)


def test_null_returns_one_value_per_seed():
    w, elig, fwd, ffund = _null_fixture()
    out = csr._null(w, elig, fwd, ffund, 0.0005, 0.0, seeds=5)
    assert len(out) == 5


# ------------------------------------------------------------------- _last_beta

def test_last_beta_excludes_rows_on_or_after_refit_and_rounds():
    """The rank-ridge is fit on strictly-before-2025-12-01 rows only.
    `fit_ridge` (in `model.cross_section`, untouched here) is monkeypatched
    to capture its input and return a known coefficient vector, so the row
    count and the 4-dp rounding are both pinned without needing a real fit."""
    idx = pd.date_range("2025-11-25", periods=10, freq="D", tz="UTC")  # spans the cutoff
    cols = ["A", "B"]
    rng = np.random.default_rng(1)
    feats = {
        "f1": pd.DataFrame(rng.uniform(size=(10, 2)), index=idx, columns=cols),
        "f2": pd.DataFrame(rng.uniform(size=(10, 2)), index=idx, columns=cols),
    }
    fwd = pd.DataFrame(rng.uniform(size=(10, 2)), index=idx, columns=cols)
    elig = pd.DataFrame(True, index=idx, columns=cols)

    captured = {}

    def fake_fit_ridge(X, y, lam=1.0):
        captured["X"] = X
        return np.array([0.123456, -0.654321])

    orig = mcs.fit_ridge
    mcs.fit_ridge = fake_fit_ridge
    try:
        out = csr._last_beta(feats, fwd, elig)
    finally:
        mcs.fit_ridge = orig

    assert out == {"f1": 0.1235, "f2": -0.6543}
    # 2025-11-25..2025-11-30 = 6 days strictly before the 2025-12-01 refit,
    # x 2 eligible names each = 12 rows. Rows on/after the cutoff (the
    # remaining 4 days) must be excluded.
    assert captured["X"].shape[0] == 12
