"""KI-095: `delta_auc_ci` bootstraps test days only. Both boosters are seeded and
deterministic, so fit variance never enters its CI, and |dAUC| under ~0.003
cannot be read without a seed sweep. `delta_auc_over_seeds` is that sweep,
opt-in: it refits per seed (via a caller-supplied `fit_oos`), calls
`delta_auc_ci` on each frame, and reports the spread of the point estimates.

Everything here is synthetic. `fit_oos` is a stub whose predictions differ by
seed in a known, hand-computable way, so no LightGBM and no archive is read.
The first two tests pin that the DEFAULT path is unchanged: `delta_auc_ci`
against literals recorded from the code before the sweep was added, and the
report path never reaching the sweep."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import cross_asset_report as car  # noqa: E402
import report_results  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)
    return tmp_path


# ------------------------------------------------------------------- fixtures

def _pinned_frame() -> pd.DataFrame:
    """30 days x 8 bars, seeded. The literals below were recorded from
    `delta_auc_ci` BEFORE `delta_auc_over_seeds` was added to the module."""
    rng = np.random.default_rng(2024)
    days = pd.date_range("2024-03-01", periods=30, freq="D", tz="UTC")
    idx = pd.DatetimeIndex([d + pd.Timedelta(minutes=15 * k)
                            for d in days for k in range(8)])
    y = rng.integers(0, 2, len(idx))
    base = np.clip(0.5 + 0.10 * (y - 0.5) + rng.normal(0, 0.15, len(idx)),
                   0.01, 0.99)
    cross = np.clip(0.5 + 0.18 * (y - 0.5) + rng.normal(0, 0.15, len(idx)),
                    0.01, 0.99)
    return pd.DataFrame({"y": y, "base": base, "cross": cross}, index=idx)


def _one_day_frame(swapped: int) -> pd.DataFrame:
    """10 positives, 10 negatives on one day. base is all ties (auc 0.5).
    cross puts positives at 1.0 and negatives at 0.0, except `swapped`
    negatives raised to 1.0 -- each ties with all 10 positives and costs
    0.5 * 10 pairs of 100, so auc(cross) = 1 - 0.05 * swapped and
    dAUC = 0.5 - 0.05 * swapped exactly. One day => the day-block
    bootstrap collapses to the point."""
    n = 20
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    y = np.array([1] * 10 + [0] * 10)
    cross = y.astype(float)
    cross[10:10 + swapped] = 1.0
    return pd.DataFrame({"y": y, "base": np.full(n, 0.5), "cross": cross},
                        index=idx)


SWAPPED_BY_SEED = {11: 0, 22: 2, 33: 6}  # dAUC 0.5, 0.4, 0.2


def _stub_fit(calls: list | None = None):
    def fit_oos(seed):
        if calls is not None:
            calls.append(seed)
        return _one_day_frame(SWAPPED_BY_SEED[seed])
    return fit_oos


def _multi_day_fit(seed: int) -> pd.DataFrame:
    """Seed-dependent predictions over 25 days, so the bootstrap CI is real
    (non-collapsed) and differs per seed."""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2024-01-01", periods=25, freq="D", tz="UTC")
    idx = pd.DatetimeIndex([d + pd.Timedelta(hours=h)
                            for d in days for h in range(6)])
    y = np.tile([1, 0, 1, 0, 1, 0], 25)
    base = rng.uniform(0.2, 0.8, len(idx))
    cross = np.clip(base + 0.2 * (y - 0.5) + rng.normal(0, 0.2, len(idx)),
                    0.01, 0.99)
    flow = rng.uniform(0.0, 1.0, len(idx))
    return pd.DataFrame({"y": y, "base": base, "cross": cross, "flow": flow},
                        index=idx)


# ----------------------------------------- default behaviour is unchanged

def test_delta_auc_ci_output_matches_values_recorded_before_the_sweep_existed():
    oos = _pinned_frame()
    got = car.delta_auc_ci(oos)
    want = (0.1076465169017079, 0.02765830736608453, 0.18825485253840343)
    assert got == pytest.approx(want, rel=0, abs=1e-12)
    flow = oos.assign(flow=oos.cross.to_numpy()[::-1])
    got_flow = car.delta_auc_ci(flow, arm="flow")
    want_flow = (-0.18028488413294597, -0.26896984880666, -0.09599500404820922)
    assert got_flow == pytest.approx(want_flow, rel=0, abs=1e-12)


def test_default_report_path_never_calls_the_sweep(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the sweep is opt-in; report must not call it")

    monkeypatch.setattr(car, "delta_auc_over_seeds", boom)
    oos = _pinned_frame()
    oos = oos.assign(year=[2022 + (i // 60) % 4 for i in range(len(oos))])
    assert car.report("BTCUSDT", oos) in {"PASS", "FAIL"}
    # static pin: nothing on the default path even names it
    for fn in (car.report, car.collect, car.main, car.delta_auc_ci):
        assert "delta_auc_over_seeds" not in fn.__code__.co_names


def test_the_seed_enters_the_fit_through_params_and_is_not_vacuous():
    """`collect` fits with `_PARAMS`; a sweep refits with {**_PARAMS, "seed": s}.
    That only varies the fit if something seeded actually samples: column
    subsampling (feature_fraction) or row bagging. If both were switched
    off, every seed would give the identical booster and the sweep's zero
    spread would be a lie."""
    assert "seed" in car._PARAMS
    assert (car._PARAMS.get("feature_fraction", 1.0) < 1.0
            or car._PARAMS.get("bagging_fraction", 1.0) < 1.0)


# ----------------------------------------------------- what the sweep returns

def test_points_follow_each_seed_in_the_order_given():
    out = car.delta_auc_over_seeds(_stub_fit(), [22, 11, 33])
    assert out["seeds"] == [22, 11, 33]
    assert out["points"] == pytest.approx([0.4, 0.5, 0.2])


def test_min_max_and_spread_are_over_all_seeds_not_positional():
    out = car.delta_auc_over_seeds(_stub_fit(), [22, 11, 33])
    assert out["min"] == pytest.approx(0.2)   # last seed
    assert out["max"] == pytest.approx(0.5)   # middle seed
    assert out["spread"] == pytest.approx(0.3)  # max - min


def test_fit_oos_is_called_once_per_seed_with_that_seed():
    calls: list[int] = []
    car.delta_auc_over_seeds(_stub_fit(calls), [33, 11, 22])
    assert calls == [33, 11, 22]


def test_different_seeds_give_different_points():
    """Guards 'the same seed reused for every run': the points must not all
    be equal when the stub's frames differ by seed."""
    out = car.delta_auc_over_seeds(_stub_fit(), [11, 22, 33])
    assert len(set(out["points"])) == 3


def test_each_seed_carries_its_own_bootstrap_ci_from_delta_auc_ci():
    seeds = [3, 5, 8]
    out = car.delta_auc_over_seeds(_multi_day_fit, seeds)
    assert len(out["cis"]) == len(out["points"]) == 3
    for seed, point, ci in zip(seeds, out["points"], out["cis"]):
        want_point, want_low, want_high = car.delta_auc_ci(_multi_day_fit(seed))
        assert point == pytest.approx(want_point, abs=1e-12)
        assert tuple(ci) == pytest.approx((want_low, want_high), abs=1e-12)
        assert ci[0] < ci[1]  # a real, non-collapsed interval
    assert len({tuple(ci) for ci in out["cis"]}) == 3  # differs per seed


def test_spread_is_fit_variance_not_bootstrap_width():
    """The same frame for every seed => no fit variance => spread 0, even
    though the day-block bootstrap CI on that frame is wide. The bootstrap
    seed inside `delta_auc_ci` is fixed, so the CIs are identical too."""
    out = car.delta_auc_over_seeds(lambda seed: _multi_day_fit(0), [1, 2, 3, 4])
    assert out["spread"] == 0.0
    assert out["min"] == out["max"]
    low, high = out["cis"][0]
    assert high - low > 0.05
    assert len({tuple(ci) for ci in out["cis"]}) == 1


def test_single_seed_has_zero_spread():
    out = car.delta_auc_over_seeds(_stub_fit(), [22])
    assert out["points"] == pytest.approx([0.4])
    assert out["min"] == out["max"] == pytest.approx(0.4)
    assert out["spread"] == 0.0


def test_arm_is_passed_through_to_delta_auc_ci():
    seeds = [3, 5]
    cross = car.delta_auc_over_seeds(_multi_day_fit, seeds)
    flow = car.delta_auc_over_seeds(_multi_day_fit, seeds, arm="flow")
    for seed, point in zip(seeds, flow["points"]):
        want, _, _ = car.delta_auc_ci(_multi_day_fit(seed), arm="flow")
        assert point == pytest.approx(want, abs=1e-12)
    assert flow["points"] != pytest.approx(cross["points"])


def test_seeds_may_be_a_one_shot_iterator():
    calls: list[int] = []
    out = car.delta_auc_over_seeds(_stub_fit(calls), iter([11, 22, 33]))
    assert calls == [11, 22, 33]
    assert out["seeds"] == [11, 22, 33]
    assert out["spread"] == pytest.approx(0.3)


# --------------------------------------------------------------------- refusals

def test_empty_seeds_are_refused_before_any_fit():
    calls: list[int] = []
    with pytest.raises(ValueError, match="at least one seed"):
        car.delta_auc_over_seeds(_stub_fit(calls), [])
    assert calls == []


def test_duplicate_seeds_are_refused_because_they_understate_the_spread():
    calls: list[int] = []
    with pytest.raises(ValueError, match="duplicate"):
        car.delta_auc_over_seeds(_stub_fit(calls), [11, 22, 11])
    assert calls == []
