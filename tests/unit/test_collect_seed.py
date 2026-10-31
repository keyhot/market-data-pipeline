"""KI-095 follow-up (a), S21 #73: a `seed` kwarg on the `collect` functions that
feed dAUC (`cross_asset_report`, `archive_report`, `funding_report`,
`order_flow_report`), so `delta_auc_over_seeds` can be driven by a real `collect`.

Everything here is synthetic: the data stages are stubbed (as in
`test_walk_forward_contract.py`), and nothing reads the archive, the bars store
or a database. Two kinds of booster stand in for `lgb`:

* a spy that records the `params` object each `lgb.train` receives and fits
  nothing, to pin WHAT reaches the booster: `seed=None` hands over the very
  `_PARAMS` object (default path unchanged), a seed hands over a fresh
  `{**_PARAMS, "seed": s}` for every fit of every arm, and `_PARAMS` itself is
  never touched;
* the same spy wrapped around the real LightGBM, on a small synthetic frame
  with ten base features (`feature_fraction` 0.9 drops one per tree, so the
  seed is not vacuous), to pin the OUTPUT: the default predictions equal
  literals recorded from the code BEFORE the kwarg existed, two seeds differ,
  one seed reproduces."""

import copy
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import lightgbm as real_lgb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from model.train import _PARAMS  # noqa: E402
import archive_report as ar  # noqa: E402
import cross_asset_report as car  # noqa: E402
import funding_report as fr  # noqa: E402
import order_flow_report as ofr  # noqa: E402

ARM = ("arm features",)  # anything but a str: a str marks the leaked control
INDEX = pd.date_range("2021-01-01", "2025-12-31", freq="D", tz="UTC")
N_BASE = 10


def _data():
    rng = np.random.default_rng(73)
    X = pd.DataFrame(rng.normal(size=(len(INDEX), N_BASE)), index=INDEX,
                     columns=[f"f{k}" for k in range(N_BASE)])
    signal = 0.6 * X["f0"] + 0.4 * X["f1"] + rng.normal(size=len(INDEX))
    y = pd.Series((signal > 0).astype(int), index=INDEX)
    extra = pd.DataFrame(rng.normal(size=(len(INDEX), 2)), index=INDEX,
                         columns=["a0", "a1"])
    leak = pd.DataFrame({"c0": signal + rng.normal(size=len(INDEX))}, index=INDEX)
    close = 100.0 * np.exp(np.cumsum(0.01 * rng.normal(size=len(INDEX))))
    bars = pd.DataFrame({"timestamp": INDEX, "open": close, "high": close,
                         "low": close, "close": close, "volume": 1.0})
    return SimpleNamespace(own=SimpleNamespace(X=X, y=y), extra=extra, leak=leak,
                           bars=bars, feats=pd.DataFrame(
                               {"premium_1": rng.normal(size=len(INDEX)),
                                "flow_imbalance_1": rng.normal(size=len(INDEX))},
                               index=INDEX))


def _with_features(data):
    """Stand-in for the `with_*_features` joins: the real arm adds `a0, a1`; the
    leaked control (marked by a str) adds `c0`. Every arm keeps every row."""
    def fake(X, y, feats):
        return X.join(data.leak if isinstance(feats, str) else data.extra), y
    return fake


class _Spy:
    """Stands in for the `lgb` module. Records the `params` object of every
    `lgb.train`, in call order. `fit=False` fits nothing and predicts 0.5."""

    def __init__(self, fit: bool):
        self.fit = fit
        self.params = []

    def Dataset(self, X, label=None):  # noqa: N802
        return real_lgb.Dataset(X, label=label) if self.fit else SimpleNamespace(X=X)

    def train(self, params, dataset, num_boost_round=None):
        self.params.append(params)
        if self.fit:
            return real_lgb.train(params, dataset, num_boost_round=num_boost_round)
        return SimpleNamespace(predict=lambda X: np.full(len(X), 0.5))


# ---------------------------------------------------------------- scenarios
# Each builds a callable `collect(**kw)` over stubbed data stages and returns
# (call, arm column that dAUC reads, fits per call). `call(**kw)` returns the
# oos frame; `kw` is `{}` (the default path) or `{"seed": s}`.

def _cross(mp, spy, data):
    mp.setattr(car, "lgb", spy)
    mp.setattr(car, "_bars", lambda symbol: "BARS")
    mp.setattr(car, "build_features", lambda b, horizon_bars: data.own)
    mp.setattr(car, "with_cross_features",
               lambda X, y, other: _with_features(data)(X, y, ARM))
    return lambda **kw: car.collect("BTCUSDT", **kw), "cross", 4 * 2


def _archive(arm, control_only=False):
    def build(mp, spy, data):
        mp.setattr(ar, "lgb", spy)
        mp.setattr(ar, "_perp_bars", lambda symbol: data.bars)
        mp.setattr(ar, "_load", lambda dataset, symbol: "RAW")
        mp.setattr(ar, "check_stamp_convention", lambda m, close: {
            "delay": pd.Timedelta(0), "delays": {}, "by_year": {}})
        mp.setattr(ar, "metrics_features", lambda m, idx, delay: ARM)
        mp.setattr(ar, "depth_features", lambda d, idx: ARM)
        mp.setattr(ar, "leaked_control", lambda f, close, col: "CTRL")
        mp.setattr(ar, "with_archive_features", _with_features(data))
        mp.setattr(ar, "build_features", lambda b, horizon_bars: data.own)
        arms = 2 if control_only else 3
        return (lambda **kw: ar.collect("BTCUSDT", arm, control_only, **kw)[0],
                arm, len(ar.ARMS[arm][2]) * arms)
    return build


def _funding(mp, spy, data):
    mp.setattr(fr, "lgb", spy)
    mp.setattr(fr, "_bars", lambda symbol: data.bars)
    mp.setattr(fr, "_load", lambda kind, symbol: [])
    mp.setattr(fr, "kline_close", lambda rows: "INDEX-CLOSE")
    mp.setattr(fr, "check_grid", lambda index_close, close: {})
    mp.setattr(fr, "funding_series", lambda rows: "SERIES")
    mp.setattr(fr, "funding_features", lambda s, p, idx: data.feats)
    mp.setattr(fr, "leaked_control", lambda f, index_close: "CTRL")
    mp.setattr(fr, "with_funding_features", _with_features(data))
    mp.setattr(fr, "build_features", lambda b, horizon_bars: data.own)
    return lambda **kw: fr.collect("BTCUSDT", **kw)[0], "funding", 4 * 3


def _flow(mp, spy, data):
    mp.setattr(ofr, "lgb", spy)
    mp.setattr(ofr, "_bars", lambda symbol: data.bars)
    mp.setattr(ofr, "_load_raw", lambda symbol: [])
    mp.setattr(ofr, "flow_frame", lambda raw: "FRAME")
    mp.setattr(ofr, "align_flow", lambda frame, bars: "FLOW")
    mp.setattr(ofr, "flow_features", lambda flow, close: data.feats)
    mp.setattr(ofr, "leaked_control", lambda f: "CTRL")
    mp.setattr(ofr, "with_flow_features", _with_features(data))
    mp.setattr(ofr, "build_features", lambda b, horizon_bars: data.own)
    return lambda **kw: ofr.collect("BTCUSDT", **kw)[0], "flow", 4 * 3


SCENARIOS = {
    "cross_asset": _cross,
    "archive/metrics": _archive("metrics"),
    "archive/depth": _archive("depth"),
    "archive/metrics control_only": _archive("metrics", control_only=True),
    "funding": _funding,
    "order_flow": _flow,
}


def _setup(monkeypatch, scenario, fit):
    spy = _Spy(fit)
    call, arm, fits = SCENARIOS[scenario](monkeypatch, spy, _data())
    return spy, call, arm, fits


def _summary(oos):
    """Per prediction column: first value, last value and the sum."""
    return {c: [float(oos[c].iloc[0]), float(oos[c].iloc[-1]), float(oos[c].sum())]
            for c in oos.columns if c not in ("year", "y", "ret")}


# Recorded by running each `collect` on this fixture BEFORE `seed` was added
# to any script (real LightGBM, `_PARAMS` and `_NUM_ROUNDS` as they are):
# [first prediction, last prediction, sum] per arm column, and the row count.
RECORDED_BEFORE_THE_KWARG = {
    "cross_asset": {
        "rows": 1461,
        "base": [0.8958387163092768, 0.6124110934520705, 719.7506239227334],
        "cross": [0.955457799771483, 0.6875295371013389, 711.6387825954439]},
    "archive/metrics": {
        "rows": 1096,
        "base": [0.8972337671756735, 0.6124110934520705, 551.9030428260048],
        "control": [0.9435968467808035, 0.39531563936080427, 543.9977974595681],
        "metrics": [0.8179747299442645, 0.6875295371013389, 542.4980757933669]},
    "archive/depth": {
        "rows": 731,
        "base": [0.42627895464322935, 0.6124110934520705, 370.49430315344677],
        "control": [0.08395454965522704, 0.39531563936080427, 359.809103936862],
        "depth": [0.38482466021293604, 0.6875295371013389, 367.1902729749726]},
    "archive/metrics control_only": {
        "rows": 1096,
        "base": [0.8972337671756735, 0.6124110934520705, 551.9030428260048],
        "control": [0.9435968467808035, 0.39531563936080427, 543.9977974595681]},
    "funding": {
        "rows": 1461,
        "base": [0.8958387163092768, 0.6124110934520705, 719.7506239227334],
        "control": [0.9686364478224413, 0.39531563936080427, 714.9053909048928],
        "funding": [0.955457799771483, 0.6875295371013389, 711.6387825954439]},
    "order_flow": {
        "rows": 1461,
        "base": [0.8958387163092768, 0.6124110934520705, 719.7506239227334],
        "control": [0.9686364478224413, 0.39531563936080427, 714.9053909048928],
        "flow": [0.955457799771483, 0.6875295371013389, 711.6387825954439]},
}
EVERY_SCENARIO = list(SCENARIOS)
SWEEPABLE = [s for s in SCENARIOS if "control_only" not in s]  # they have an arm column
COLLECTS = {"cross_asset": car.collect, "archive": ar.collect,
            "funding": fr.collect, "order_flow": ofr.collect}


# ------------------------------------------------------- default path unchanged

@pytest.mark.parametrize("scenario", EVERY_SCENARIO)
def test_default_collect_equals_values_recorded_before_the_kwarg(monkeypatch, scenario):
    _, call, _, _ = _setup(monkeypatch, scenario, fit=True)
    oos = call()
    want = RECORDED_BEFORE_THE_KWARG[scenario]
    assert len(oos) == want["rows"]
    got = _summary(oos)
    assert set(got) == set(want) - {"rows"}
    for col, values in got.items():
        assert values == pytest.approx(want[col], rel=1e-9), col


@pytest.mark.parametrize("scenario", EVERY_SCENARIO)
def test_seed_none_hands_every_fit_the_very_params_object(monkeypatch, scenario):
    for kw in ({}, {"seed": None}):
        spy, call, _, fits = _setup(monkeypatch, scenario, fit=False)
        call(**kw)
        assert len(spy.params) == fits
        assert all(p is _PARAMS for p in spy.params), kw
        monkeypatch.undo()


@pytest.mark.parametrize("name, fn", [*COLLECTS.items(),
                                      ("archive._walk_forward", ar._walk_forward)])
def test_seed_defaults_to_none_and_is_the_last_parameter(name, fn):
    params = inspect.signature(fn).parameters
    assert params["seed"].default is None
    assert list(params)[-1] == "seed"


def test_existing_positional_calls_keep_their_meaning():
    """`seed` is appended: nobody who passed `legacy_delay` positionally (as
    `archive_report.main` does) is moved."""
    assert list(inspect.signature(ar.collect).parameters) == [
        "symbol", "arm", "control_only", "legacy_delay", "seed"]
    assert list(inspect.signature(car.collect).parameters) == ["symbol", "seed"]
    assert list(inspect.signature(ar._walk_forward).parameters) == [
        "arms", "y", "forward", "years", "seed"]


# ------------------------------------------------------- a seed reaches every fit

@pytest.mark.parametrize("seed", [7, 0])  # 0 is falsy: `if seed` would drop it
@pytest.mark.parametrize("scenario", EVERY_SCENARIO)
def test_a_seed_reaches_every_fit_of_every_arm(monkeypatch, scenario, seed):
    spy, call, _, fits = _setup(monkeypatch, scenario, fit=False)
    call(seed=seed)
    assert len(spy.params) == fits  # every year x every arm, none skipped
    for p in spy.params:
        assert p == {**_PARAMS, "seed": seed}
        assert p is not _PARAMS


@pytest.mark.parametrize("scenario", EVERY_SCENARIO)
def test_params_is_not_mutated_and_does_not_leak_into_the_next_call(
        monkeypatch, scenario):
    before = copy.deepcopy(_PARAMS)
    spy, call, _, fits = _setup(monkeypatch, scenario, fit=False)
    call(seed=123)
    assert _PARAMS == before and _PARAMS["seed"] == 42
    spy.params.clear()
    call()  # the default call after a seeded one still gets the original
    assert len(spy.params) == fits
    assert all(p is _PARAMS and p["seed"] == 42 for p in spy.params)
    assert _PARAMS == before


# ----------------------------------------- output: same seed repeats, seeds differ

@pytest.mark.parametrize("scenario", EVERY_SCENARIO)
def test_same_seed_reproduces_and_two_seeds_differ_in_every_arm(monkeypatch, scenario):
    _, call, _, _ = _setup(monkeypatch, scenario, fit=True)
    one, again, other = call(seed=1), call(seed=1), call(seed=2)
    pd.testing.assert_frame_equal(one, again, check_exact=True)
    for col in _summary(one):
        assert not np.array_equal(one[col], other[col]), f"{col} ignored the seed"


@pytest.mark.parametrize("scenario", EVERY_SCENARIO)
def test_default_is_the_seeded_path_at_the_original_seed_42(monkeypatch, scenario):
    """`_PARAMS["seed"]` is 42, so `seed=42` must reproduce the default frame
    exactly, and any other seed must not."""
    _, call, _, _ = _setup(monkeypatch, scenario, fit=True)
    default = call()
    pd.testing.assert_frame_equal(call(seed=42), default, check_exact=True)
    other = call(seed=43)
    assert any(not np.array_equal(default[c], other[c]) for c in _summary(default))


# ------------------------------------------------ delta_auc_over_seeds on collect

@pytest.mark.parametrize("scenario", SWEEPABLE)
def test_delta_auc_over_seeds_runs_on_real_collect_calls(monkeypatch, scenario):
    """The point of the kwarg: `fit_oos = lambda s: collect(..., seed=s)` is a
    working `fit_oos`. Each collect's output maps to the sweep's `arm` as the
    `collect` docstrings state: cross_asset "cross", archive "metrics" /
    "depth", funding "funding", order_flow "flow"."""
    monkeypatch.setattr(car, "BOOTSTRAP", 50)
    _, call, arm, _ = _setup(monkeypatch, scenario, fit=True)
    calls = []

    def fit_oos(seed):
        calls.append(seed)
        return call(seed=seed)

    out = car.delta_auc_over_seeds(fit_oos, [42, 1, 2], arm=arm)
    assert calls == [42, 1, 2]
    assert out["seeds"] == [42, 1, 2]
    assert len(set(out["points"])) == 3, "each seed refit changed the dAUC"
    assert out["spread"] > 0
    # seed 42 is the default fit, so its point is the report's own dAUC
    assert out["points"][0] == car.delta_auc_ci(call(), arm)[0]
    again = car.delta_auc_over_seeds(fit_oos, [42, 1, 2], arm=arm)
    assert again == out, "the sweep reproduces"


def test_control_only_frames_have_no_arm_column_to_sweep(monkeypatch):
    _, call, _, _ = _setup(monkeypatch, "archive/metrics control_only", fit=False)
    assert "metrics" not in call(seed=1).columns
