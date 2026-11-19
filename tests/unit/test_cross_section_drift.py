"""KI-101 / S19 #56: `portfolio_pnl(drift=True)` charges turnover against
weights drifted by the day's own return. Opt-in: the default is the frozen
E13 coding and is pinned here against values recorded from main BEFORE the
edit. The drift tests are hand-computed, not read back from the code."""

import inspect
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import e13_diagnostics as e13  # noqa: E402

import model.cross_section as mcs  # noqa: E402
from model.cross_section import (  # noqa: E402
    decile_weights,
    drifted_weights,
    portfolio_pnl,
)


def _days(n, start="2024-03-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


# ----------------------------------------------------------- the default --

def _seeded_panel():
    """8 days x 5 names: arbitrary (non-unit-leg) weights, a flat row, a NaN
    weight, a NaN return, a NaN slippage cell."""
    rng = np.random.default_rng(56)
    days, cols = _days(8), list("ABCDE")
    w = pd.DataFrame(np.round(rng.standard_normal((8, 5)), 1), days, cols)
    w.iloc[3] = 0.0
    w.iloc[5, 2] = np.nan
    r = pd.DataFrame(0.02 * rng.standard_normal((8, 5)), days, cols)
    r.iloc[2, 1] = np.nan
    f = pd.DataFrame(0.0005 * rng.standard_normal((8, 5)), days, cols)
    slip = pd.DataFrame(0.0002 * np.abs(rng.standard_normal((8, 5))), days, cols)
    slip.iloc[4, 0] = np.nan
    return w, r, f, slip


# Recorded from `portfolio_pnl` as it stood on main (1493521; #54 leaves the
# function untouched) before this change, with repr() floats.
_MAIN_FRAME_SLIPPAGE = {
    "gross": [
        3.8125944112083615e-05,
        0.04309155883172868,
        0.08230858564594186,
        0.0,
        -0.006893616378829343,
        -0.016952803275989685,
        -0.00747876514455351,
        -0.003567670641669272,
    ],
    "funding": [
        -0.00021497919750273857,
        -3.3844018786568956e-05,
        -0.00046467134062366024,
        -0.0,
        -0.0005282365672908483,
        -0.0015004865689926482,
        -0.0004441475260090884,
        -0.0003043887302520832,
    ],
    "cost": [
        0.002986131706447983,
        0.0037384664943827767,
        0.0038531532661937036,
        0.0023248874147675912,
        0.0030510461422787625,
        0.0009043685572343402,
        0.0009589743188803087,
        0.002460549892485155,
    ],
    "turnover": [
        4.2,
        5.5,
        6.1000000000000005,
        3.5999999999999996,
        4.6000000000000005,
        1.4000000000000001,
        1.4000000000000001,
        3.7,
    ],
    "net": [
        -0.003162984959838638,
        0.03931924831855934,
        0.0779907610391245,
        -0.0023248874147675912,
        -0.010472899088398954,
        -0.019357658402216673,
        -0.008881886989442906,
        -0.00633260926440651,
    ],
}
_MAIN_SCALAR_SLIPPAGE = {
    "gross": [
        3.8125944112083615e-05,
        0.04309155883172868,
        0.08230858564594186,
        0.0,
        -0.006893616378829343,
        -0.016952803275989685,
        -0.00747876514455351,
        -0.003567670641669272,
    ],
    "funding": [
        -0.00021497919750273857,
        -3.3844018786568956e-05,
        -0.00046467134062366024,
        -0.0,
        -0.0005282365672908483,
        -0.0015004865689926482,
        -0.0004441475260090884,
        -0.0003043887302520832,
    ],
    "cost": [
        0.0025200000000000005,
        0.0033000000000000004,
        0.00366,
        0.0021600000000000005,
        0.0027600000000000003,
        0.0008400000000000001,
        0.0008400000000000001,
        0.0022199999999999998,
    ],
    "turnover": [
        4.2,
        5.5,
        6.1000000000000005,
        3.5999999999999996,
        4.6000000000000005,
        1.4000000000000001,
        1.4000000000000001,
        3.7,
    ],
    "net": [
        -0.0026968532533906556,
        0.039757714812942116,
        0.07818391430531821,
        -0.0021600000000000005,
        -0.01018185294612019,
        -0.019293289844982332,
        -0.008762912670562598,
        -0.006092059371921355,
    ],
}


def _assert_bit_identical(out, ref):
    assert (
        list(out.columns)
        == list(ref)
        == ["gross", "funding", "cost", "turnover", "net"]
    )
    for col, want in ref.items():
        assert out[col].tolist() == want, col          # == on floats: bit-identical


def test_default_output_is_bit_identical_to_the_values_recorded_from_main():
    w, r, f, slip = _seeded_panel()
    _assert_bit_identical(portfolio_pnl(w, r, f, 0.0005, slip), _MAIN_FRAME_SLIPPAGE)
    _assert_bit_identical(portfolio_pnl(w, r, f, 0.0005, 0.0001), _MAIN_SCALAR_SLIPPAGE)


def test_an_explicit_drift_false_is_the_default_and_drift_is_keyword_only():
    w, r, f, slip = _seeded_panel()
    pd.testing.assert_frame_equal(
        portfolio_pnl(w, r, f, 0.0005, slip, drift=False),
        portfolio_pnl(w, r, f, 0.0005, slip),
        check_exact=True,
    )
    with pytest.raises(TypeError):
        portfolio_pnl(w, r, f, 0.0005, slip, True)


def test_the_default_path_never_builds_the_drifted_book(monkeypatch):
    w, r, f, slip = _seeded_panel()

    def boom(*a, **k):
        raise RuntimeError("drifted_weights called on the default path")
    monkeypatch.setattr(mcs, "drifted_weights", boom)
    portfolio_pnl(w, r, f, 0.0005, slip)
    portfolio_pnl(w, r, f, 0.0005, slip, drift=False)
    with pytest.raises(RuntimeError):
        portfolio_pnl(w, r, f, 0.0005, slip, drift=True)


# --------------------------------------------------- hand-computed drift --
# Six days, three names. Long book A, B (+0.5 each), short book C (-1).
#   d0  A .5  B .5  C -1       r0: A +50%  B 0     C +10%
#   d1  A .5  B .5  C -1       r1: A -20%  B +10%  C NaN   (a held name, NaN return)
#   d2  A -.5 B 1   C -.5      r2: A +10%  B -10%  C +40%  (A flips long -> short)
#   d3  flat                   r3: all +5%
#   d4  A 1   B -1  C NaN      r4: A -100% (wiped out) B +20%  (NaN weight = 0)
#   d5  A .25 B -.8 C .75      r5: 0
# Drifted book at each d (w_{d-1} * (1 + r_{d-1}), rescaled per leg to its
# prior gross):
#   d1  longs A .75 B .5 -> /1.25 -> A .6 B .4;  short C -1.1 -> -1
#   d2  longs A .4 B .55 -> /.95 -> A 8/19 B 11/19;  C NaN -> 0 drift -> -1
#   d3  long B .9 -> 1;  shorts A -.55 C -.7 -> /-1.25 * -1 -> A -.44 C -.56
#   d4  flat yesterday -> drifted book is empty
#   d5  long leg A wiped out -> 0;  short B -1.2 -> -1
# traded (drift):    d0 2   d1 .2   d2 35/19  d3 2   d4 2   d5 1.2
# traded (default):  d0 2   d1 0    d2 2      d3 2   d4 2   d5 1.7

_FEE = 0.0005
_SLIP = {"A": 0.0001, "B": 0.0002, "C": 0.0004}


def _hand_panel():
    days, cols = _days(6), list("ABC")
    nan = np.nan
    w = pd.DataFrame(
        [
            [0.5, 0.5, -1],
            [0.5, 0.5, -1],
            [-0.5, 1, -0.5],
            [0, 0, 0],
            [1, -1, nan],
            [0.25, -0.8, 0.75],
        ],
        days,
        cols,
    )
    r = pd.DataFrame([[.5, 0, .1], [-.2, .1, nan], [.1, -.1, .4], [.05, .05, .05],
                      [-1, .2, 0], [0, 0, 0]], days, cols)
    f = pd.DataFrame([[.001, .002, -.001]] * 6, days, cols)
    slip = pd.DataFrame([[_SLIP[c] for c in cols]] * 6, days, cols)
    return w, r, f, slip


_HAND_TRADED_DRIFT = [2.0, 0.2, 35 / 19, 2.0, 2.0, 1.2]
_HAND_TRADED_DEFAULT = [2.0, 0.0, 2.0, 2.0, 2.0, 1.7]


def test_drift_turnover_on_the_hand_computed_panel():
    w, r, f, slip = _hand_panel()
    drift = portfolio_pnl(w, r, f, _FEE, slip, drift=True)
    default = portfolio_pnl(w, r, f, _FEE, slip)
    assert drift["turnover"].tolist() == pytest.approx(_HAND_TRADED_DRIFT, abs=1e-12)
    assert default["turnover"].tolist() == pytest.approx(
        _HAND_TRADED_DEFAULT, abs=1e-12
    )


def test_drift_cost_is_charged_per_name_on_the_drifted_trade():
    w, r, f, slip = _hand_panel()
    fa, fb, fc = (_FEE + _SLIP[c] for c in "ABC")  # per-name fee + slippage
    want = [
        0.5 * fa + 0.5 * fb + 1.0 * fc,  # d0: the whole book
        0.1 * fa + 0.1 * fb,  # d1: A -> .6, B -> .4 drifted
        (35 / 38) * fa + (8 / 19) * fb + 0.5 * fc,  # d2
        0.44 * fa + 1.0 * fb + 0.56 * fc,  # d3: flat target
        1.0 * fa + 1.0 * fb,  # d4: yesterday flat; NaN = 0
        0.25 * fa + 0.2 * fb + 0.75 * fc,
    ]                         # d5: wiped-out leg
    got = portfolio_pnl(w, r, f, _FEE, slip, drift=True)
    assert got["cost"].tolist() == pytest.approx(want, abs=1e-15)
    assert (
        got["net"] - (got["gross"] + got["funding"] - got["cost"])
    ).abs().max() < 1e-15


def test_drift_changes_only_what_trades_not_gross_or_funding():
    w, r, f, slip = _hand_panel()
    drift = portfolio_pnl(w, r, f, _FEE, slip, drift=True)
    default = portfolio_pnl(w, r, f, _FEE, slip)
    pd.testing.assert_series_equal(drift["gross"], default["gross"], check_exact=True)
    pd.testing.assert_series_equal(
        drift["funding"], default["funding"], check_exact=True
    )
    assert drift["gross"].iloc[0] == pytest.approx(
        0.5 * 0.5 + 0 + -1 * 0.1
    )  # d0 gross, by hand


def test_drifted_weights_hand_values():
    w, r, _, _ = _hand_panel()
    got = drifted_weights(w, r)
    want = np.array([[0, 0, 0], [.6, .4, -1], [8 / 19, 11 / 19, -1], [-.44, 1.0, -.56],
                     [0, 0, 0], [0, -1, 0]])
    np.testing.assert_allclose(got.to_numpy(), want, atol=1e-15)
    assert np.isfinite(got.to_numpy()).all()  # the wiped-out leg is 0, not NaN / inf


def test_a_leg_keeps_its_prior_gross_not_a_forced_one():
    days = _days(2)
    w = pd.DataFrame([[.25, .25, -.5], [.3, .2, -.5]], days, list("ABC"))
    r = pd.DataFrame([[.5, 0.0, 0.0], [0.0, 0.0, 0.0]], days, list("ABC"))
    # grown .375 / .25 -> rescaled to the long leg's prior gross .5: .3 / .2
    np.testing.assert_allclose(drifted_weights(w, r).iloc[1].to_numpy(), [.3, .2, -.5])
    z = pd.DataFrame(0.0, days, w.columns)
    assert portfolio_pnl(w, r, z, 0, 0, drift=True)["turnover"].iloc[
        1
    ] == pytest.approx(0.0, abs=1e-15)


def test_drift_uses_the_previous_days_return_never_a_later_one():
    # Rule: the return of row k moves the turnover of rows > k only. The
    # drift at day d is w_{d-1} grown by row d-1's forward return.
    w, r, f, slip = _hand_panel()
    base = portfolio_pnl(w, r, f, _FEE, slip, drift=True)["turnover"]
    for k in range(len(w)):
        r2 = r.copy()
        r2.iloc[k] = r2.iloc[k] + [.3, -.2, .15]
        moved = portfolio_pnl(w, r2, f, _FEE, slip, drift=True)["turnover"]
        np.testing.assert_allclose(moved.iloc[:k + 1], base.iloc[:k + 1], atol=1e-15)
    r3 = r.copy()
    r3.iloc[1] = [.9, -.5, .3]  # d2's drift is built from this row
    assert not np.isclose(
        portfolio_pnl(w, r3, f, _FEE, slip, drift=True)["turnover"].iloc[2],
        base.iloc[2],
    )


def test_equal_returns_inside_a_leg_drift_nothing_between_names():
    days = _days(3)
    w = pd.DataFrame([[.5, .5, -.5, -.5]] * 3, days, list("ABCD"))
    r = pd.DataFrame([[.1, .1, -.2, -.2]] * 3, days, list("ABCD"))
    z = pd.DataFrame(0.0, days, w.columns)
    np.testing.assert_allclose(portfolio_pnl(w, r, z, 0, 0, drift=True)["turnover"],
                               portfolio_pnl(w, r, z, 0, 0)["turnover"], atol=1e-15)


# --------------------------------------- one definition, shared with E13 --

def _old_q4_inline_drift_turnover(w, fwd):
    """The construction exactly as `scripts/e13_diagnostics.py` q4 carried it
    inline before it moved into `model.cross_section`."""
    wl, ws = w.clip(lower=0), w.clip(upper=0)
    r_prev = fwd.shift(1)
    dl = wl.shift(1) * (1 + r_prev)
    dl = dl.div(dl.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    ds = ws.shift(1) * (1 + r_prev)
    ds = -ds.div(ds.sum(axis=1).replace(0, np.nan), axis=0).abs().fillna(0.0)
    return (wl - dl).abs().sum(axis=1) + (ws - ds).abs().sum(axis=1)


def _decile_book(n_days=90, n_names=14, seed=3, frac=0.2):
    rng = np.random.default_rng(seed)
    days, cols = _days(n_days, "2023-01-01"), [f"S{i}" for i in range(n_names)]
    scores = pd.DataFrame(rng.standard_normal((n_days, n_names)), days, cols)
    scores.iloc[10:13] = np.nan                   # flat days
    scores.iloc[20, :9] = np.nan                  # too few scored names: flat
    fwd = pd.DataFrame(0.04 * rng.standard_normal((n_days, n_names)), days, cols)
    return decile_weights(scores, frac=frac), fwd


def test_the_helper_reproduces_the_construction_e13_q4_carried_inline():
    w, fwd = _decile_book()
    assert (w.abs().sum(axis=1) == 0).sum() >= 4
    old = _old_q4_inline_drift_turnover(w, fwd)
    new = (w - drifted_weights(w, fwd)).abs().sum(axis=1)
    np.testing.assert_allclose(new.to_numpy(), old.to_numpy(), atol=1e-12)
    z = pd.DataFrame(0.0, w.index, w.columns)
    got = portfolio_pnl(w, fwd, z, 0, 0, drift=True)["turnover"]
    np.testing.assert_allclose(got.to_numpy(), old.to_numpy(), atol=1e-12)
    assert (got > portfolio_pnl(w, fwd, z, 0, 0)["turnover"] + 1e-9).sum() > 20


def _nan_return_book():
    """`_decile_book` with held names whose next close is missing (NaN return),
    the case q4 reports as `held_next_close_missing`."""
    w, fwd = _decile_book()
    held_days = [d for d in range(30, 80) if (w.iloc[d] != 0).any()]
    for i, d in enumerate(held_days[::2]):
        held = np.flatnonzero(w.iloc[d].to_numpy() != 0)
        fwd.iloc[d, held[i % len(held)]] = np.nan
    fwd.iloc[40, 0] = np.nan                       # an unheld-or-held stray
    fwd.iloc[55] = np.nan                          # a whole row missing
    assert fwd.isna().to_numpy().sum() > 15
    return w, fwd


def test_q4_drift_turnover_matches_the_old_inline_code_on_nan_returns():
    # F1 (review of #56): a held name with a NaN return is DROPPED by the old
    # inline code (weight 0, the others rescaled to the leg's gross), not
    # treated as a 0% return.
    w, fwd = _nan_return_book()
    old = _old_q4_inline_drift_turnover(w, fwd)
    np.testing.assert_allclose(e13._drift_turnover(w, fwd).to_numpy(),
                               old.to_numpy(), atol=1e-12)
    # the NaN-free book still matches, and the NaNs matter (not a vacuous case)
    w0, fwd0 = _decile_book()
    np.testing.assert_allclose(e13._drift_turnover(w0, fwd0).to_numpy(),
                               _old_q4_inline_drift_turnover(w0, fwd0).to_numpy(),
                               atol=1e-12)
    as_zero = (w - drifted_weights(w, fwd)).abs().sum(axis=1)
    assert (as_zero - old).abs().max() > 1e-3


def test_q4_drift_turnover_review_probe_nan_on_a_held_name():
    # The reviewer's probe: A, B long, C short; A's return is NaN on day 0.
    # Old: A drops out, B takes the whole long leg (1.0), turnover 1.0.
    days = _days(3)
    w = pd.DataFrame([[.5, .5, -1.0]] * 3, days, list("ABC"))
    fwd = pd.DataFrame([[np.nan, .1, -.1]] + [[.0, .0, .0]] * 2, days, list("ABC"))
    old = _old_q4_inline_drift_turnover(w, fwd)
    assert old.iloc[1] == pytest.approx(1.0)
    assert e13._drift_turnover(w, fwd).iloc[1] == pytest.approx(1.0)
    assert (w - drifted_weights(w, fwd)).abs().sum(axis=1).iloc[1] == pytest.approx(
        1 / 21, rel=1e-3)   # the default NaN-as-0 reading the other callers keep


def test_other_callers_keep_nan_return_as_zero():
    # portfolio_pnl fills NaN returns with 0 for gross, so its drift path must
    # too: drift=True on a NaN panel equals drift=True on the 0-filled panel,
    # and the default `drifted_weights` reading is unchanged.
    w, fwd = _nan_return_book()
    z = pd.DataFrame(0.0, w.index, w.columns)
    got = portfolio_pnl(w, fwd, z, 0, 0, drift=True)["turnover"]
    want = (w - drifted_weights(w, fwd.fillna(0.0))).abs().sum(axis=1)
    np.testing.assert_allclose(got.to_numpy(), want.to_numpy(), atol=1e-15)
    np.testing.assert_allclose(drifted_weights(w, fwd).to_numpy(),
                               drifted_weights(w, fwd.fillna(0.0)).to_numpy(), atol=1e-15)


def test_q4_takes_its_drift_turnover_from_the_shared_helper_not_an_inline_copy():
    # q4 needs the archive panels, so it is not run here; this pins that its
    # drift number comes from `drifted_weights` (one definition, KI-101).
    src = inspect.getsource(e13.q4)
    assert "_drift_turnover(w, fwd)" in src
    assert "r_prev" not in src and "dl = " not in src and "ds = " not in src
    assert "drifted_weights" in inspect.getsource(e13._drift_turnover)


# ----------------------------------- the declared post-hoc E13 re-score --

def _e13_context():
    """Synthetic stand-in for `e13_diagnostics.build()`: the keys the re-score
    reads, with the book inside the test window."""
    w, fwd = _decile_book(n_days=80, n_names=12, seed=9)
    rng = np.random.default_rng(10)
    ffund = pd.DataFrame(0.0003 * rng.standard_normal(w.shape), w.index, w.columns)
    slip = pd.DataFrame(0.0002, w.index, w.columns)
    return dict(w=w, fwd=fwd, ffund=ffund, slip=slip, fee=0.0005)


def test_drift_rescore_reports_coded_and_drifted_rows_on_the_same_book():
    c = _e13_context()
    out = e13.drift_rescore(c)
    assert set(out) == {"coded", "drift_rebalanced", "drift_minus_coded"}
    coded, drifted, d = out["coded"], out["drift_rebalanced"], out["drift_minus_coded"]
    test = slice(e13.R.TEST_START, e13.R.TEST_END)
    ref = portfolio_pnl(c["w"], c["fwd"], c["ffund"], c["fee"], c["slip"]).loc[test]
    assert coded == e13.R._stats(ref)
    assert drifted["gross_bps"] == coded["gross_bps"]
    assert drifted["funding_bps"] == coded["funding_bps"]
    old = _old_q4_inline_drift_turnover(c["w"], c["fwd"]).loc[test]
    assert drifted["turnover"] == round(float(old.mean()), 3)
    assert d["turnover"] > 0 and d["cost_bps"] > 0 and d["net_mean_bps"] < 0
    assert d["turnover"] == round(drifted["turnover"] - coded["turnover"], 3)


def test_the_drift_rescore_flag_runs_only_the_rescore_after_the_reproduction(
        monkeypatch, tmp_path):
    c = _e13_context()
    calls = []
    monkeypatch.setattr(e13, "build", lambda: c)
    monkeypatch.setattr(
        e13, "reproduce", lambda ctx: calls.append("reproduce") or {"ok": 1}
    )
    for name in ("q1", "q2", "q3", "q4"):
        monkeypatch.setattr(e13, name, lambda *a, _n=name, **k: calls.append(_n) or {})
    out = tmp_path / "rescore.json"
    monkeypatch.setattr(
        sys, "argv", ["e13_diagnostics.py", "--drift-rescore", "--out", str(out)]
    )
    e13.main()
    res = json.loads(out.read_text())
    assert calls == ["reproduce"]
    assert list(res) == ["reproduced_stage_b", "KI101_drift_rescore"]
    assert res["KI101_drift_rescore"]["drift_minus_coded"]["turnover"] > 0


def test_without_the_flag_the_e13_diagnostics_run_exactly_as_before(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(e13, "build", lambda: {})
    monkeypatch.setattr(e13, "reproduce", lambda ctx: {})
    for name in ("q1", "q2", "q3", "q4"):
        monkeypatch.setattr(e13, name, lambda *a, _n=name, **k: calls.append(_n) or {})
    monkeypatch.setattr(e13, "drift_rescore", lambda ctx: calls.append("drift") or {})
    monkeypatch.setattr(
        sys, "argv", ["e13_diagnostics.py", "--out", str(tmp_path / "d.json")]
    )
    e13.main()
    assert calls == ["q2", "q1", "q3", "q4"]
