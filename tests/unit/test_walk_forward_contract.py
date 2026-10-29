"""KI-094 remainder: the purge and same-rows fold contract of the walk-forward
loops in `archive_report` (`_walk_forward`, `collect`), `cross_asset_report`,
`funding_report` and `order_flow_report` (`collect`). The four copies are the
same loop and had no test: an off-by-one in `train_end = test[0] - HORIZON`
leaks the label that peeks `HORIZON` bars past the last training row into
every fold of E7-E12 and would still produce a plausible AUC.

LightGBM is replaced by a recorder (`_RecordingLgb`), and the data stages by
stubs, so each fit's exact training rows, labels and columns are visible and
every arm's predictions land on a known set of test rows. Nothing here reads
the archive, the bars store or a database.

Pinned, per module: the training window is exactly the rows before
`test[0] - HORIZON` (gap -1/+1/0 all change it); the test fold is exactly the
rows of the test year; labels travel with the rows; the base arm trains on the
base columns only; every arm is fitted and scored on the SAME rows (the
intersection of the arm and control row sets, not the union or either alone);
the forward return and, for E12, `row_share` are computed on those rows."""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import archive_report as ar  # noqa: E402
import cross_asset_report as car  # noqa: E402
import funding_report as fr  # noqa: E402
import live_gate_report as lgr  # noqa: E402
import order_flow_report as ofr  # noqa: E402

# 5 calendar years x 4 daily rows. id i sits in year 2021 + i // 4, so the
# first row of 2022 is id 4, of 2023 id 8, and so on.
IDS = list(range(20))
INDEX = pd.DatetimeIndex([pd.Timestamp(year=2021 + i // 4, month=1, day=1 + i % 4, tz="UTC")
                          for i in IDS])
CLOSE = [100.0 + i ** 2 for i in IDS]
LABEL = 1000  # y(id) = LABEL + id, so a label proves which row it belongs to
ARM_DROP, CTRL_DROP = {5}, {10}  # a row only the control lacks / only the arm lacks
TEST_YEARS_ALL = (2022, 2023, 2024, 2025)
FEATS = ("arm features",)  # anything but a str; a str marks the leaked control


def year_of(i):
    return 2021 + i // 4


def _bars():
    return pd.DataFrame({"timestamp": INDEX, "open": CLOSE, "high": CLOSE,
                         "low": CLOSE, "close": CLOSE, "volume": 1.0})


def _own():
    X = pd.DataFrame({"id": [float(i) for i in IDS], "f1": 0.0}, index=INDEX)
    return SimpleNamespace(X=X, y=pd.Series([LABEL + i for i in IDS], index=INDEX))


def _with_features(arm_drop, ctrl_drop):
    """Stand-in for `with_*_features`: keep the rows not in the drop set for
    this feature kind, add an `extra` column that is 1.0 for the real arm and
    2.0 for the leaked control (a string marker stands for the control)."""
    def fake(X, y, feats):
        ctrl = isinstance(feats, str)
        drop = ctrl_drop if ctrl else arm_drop
        keep = [k for k, i in enumerate(IDS) if i not in drop]
        return X.iloc[keep].assign(extra=2.0 if ctrl else 1.0), y.iloc[keep]
    return fake


class _RecordingLgb:
    """Records every `lgb.Dataset`/`lgb.train` pair. A booster predicts
    id + 1000 * extra for the rows it is given, so a prediction column shows
    which arm's matrix scored it (base: id, arm: id+1000, control: id+2000)."""

    def __init__(self):
        self.fits = []

    def Dataset(self, X, label=None):  # noqa: N802
        return SimpleNamespace(X=X, label=label)

    def train(self, params, dataset, num_boost_round=None):
        self.fits.append(dataset)

        def predict(X):
            bump = 1000 * X["extra"].to_numpy() if "extra" in X else 0.0
            return X["id"].to_numpy() + bump
        return SimpleNamespace(predict=predict)


def _expected(rows_ids, test_years):
    """(year, train ids, test ids) for the contract: train on every row before
    the test year's first row minus one HORIZON, test on the test year."""
    out = []
    for yr in test_years:
        pos = [k for k, i in enumerate(rows_ids) if year_of(i) == yr]
        out.append((yr, rows_ids[:pos[0] - 1], [rows_ids[k] for k in pos]))
    return out


BASE_COLS = ["id", "f1"]
FULL_COLS = ["id", "f1", "extra"]


def _check(oos, fits, arms, rows_ids, test_years, with_ret=True):
    """`arms`: ordered (name, columns, bump) in the order the module fits them."""
    exp = _expected(rows_ids, test_years)
    test_ids = [i for _, _, t in exp for i in t]
    assert list(oos.index) == list(INDEX[test_ids])
    assert oos.year.tolist() == [year_of(i) for i in test_ids]
    assert oos.y.tolist() == [LABEL + i for i in test_ids]
    for name, _, bump in arms:
        assert oos[name].tolist() == [i + bump for i in test_ids], name
    if with_ret:
        want = [(CLOSE[i + 1] / CLOSE[i] - 1.0) if i + 1 < len(CLOSE) else np.nan
                for i in test_ids]
        np.testing.assert_allclose(oos.ret.to_numpy(), want, equal_nan=True)
    assert len(fits) == len(exp) * len(arms)
    k = 0
    for _, train_ids, _ in exp:
        for name, cols, _ in arms:
            ds = fits[k]
            k += 1
            assert ds.X["id"].astype(int).tolist() == train_ids, name
            assert ds.label.tolist() == [LABEL + i for i in train_ids], name
            assert list(ds.X.columns) == cols, name


# ------------------------------------------------------------------ runners

def _run_walk_forward(monkeypatch):
    rec = _RecordingLgb()
    monkeypatch.setattr(ar, "lgb", rec)
    own = _own()
    X_base = own.X
    X_arm = own.X.assign(extra=1.0)
    X_ctrl = own.X.assign(extra=2.0)
    forward = pd.Series([CLOSE[i + 1] / CLOSE[i] - 1.0 if i + 1 < 20 else np.nan
                         for i in IDS], index=INDEX)
    arms = {"base": X_base, "control": X_ctrl, "metrics": X_arm}
    oos = ar._walk_forward(arms, own.y, forward, TEST_YEARS_ALL)
    spec = [("base", BASE_COLS, 0), ("control", FULL_COLS, 2000),
            ("metrics", FULL_COLS, 1000)]
    return oos, rec.fits, spec, IDS, TEST_YEARS_ALL, True


def _run_archive(arm, control_only=False):
    def run(monkeypatch):
        rec = _RecordingLgb()
        monkeypatch.setattr(ar, "lgb", rec)
        own = _own()
        monkeypatch.setattr(ar, "_perp_bars", lambda symbol: _bars())
        monkeypatch.setattr(ar, "_load", lambda dataset, symbol: "RAW")
        monkeypatch.setattr(ar, "check_stamp_convention",
                            lambda metrics, close: {"delay": 2})
        monkeypatch.setattr(ar, "metrics_features", lambda m, idx, delay: FEATS)
        monkeypatch.setattr(ar, "depth_features", lambda d, idx: FEATS)
        monkeypatch.setattr(ar, "leaked_control", lambda feats, close, col: "CTRL")
        monkeypatch.setattr(ar, "with_archive_features",
                            _with_features(ARM_DROP, CTRL_DROP))
        monkeypatch.setattr(ar, "build_features", lambda bars, horizon_bars: own)
        oos, info = ar.collect("BTCUSDT", arm, control_only)
        rows = [i for i in IDS if i not in ARM_DROP | CTRL_DROP]
        spec = [("base", BASE_COLS, 0), ("control", FULL_COLS, 2000)]
        if not control_only:
            spec.append((arm, FULL_COLS, 1000))
        return oos, rec.fits, spec, rows, ar.ARMS[arm][2], True
    return run


def _run_cross(monkeypatch):
    rec = _RecordingLgb()
    monkeypatch.setattr(car, "lgb", rec)
    own = _own()
    bars = {"BTCUSDT": _bars(), "ETHUSDT": "ETH-BARS"}
    monkeypatch.setattr(car, "_bars", lambda symbol: bars[symbol])

    def with_cross(X, y, other):
        assert other == "ETH-BARS", "the cross features come from the OTHER asset"
        return _with_features(ARM_DROP, ARM_DROP)(X, y, FEATS)
    monkeypatch.setattr(car, "with_cross_features", with_cross)
    monkeypatch.setattr(car, "build_features", lambda b, horizon_bars: own)
    oos = car.collect("BTCUSDT")
    rows = [i for i in IDS if i not in ARM_DROP]
    spec = [("base", BASE_COLS, 0), ("cross", FULL_COLS, 1000)]
    return oos, rec.fits, spec, rows, TEST_YEARS_ALL, False


def _run_funding(monkeypatch):
    rec = _RecordingLgb()
    monkeypatch.setattr(fr, "lgb", rec)
    own = _own()
    rng = np.random.default_rng(0)
    feats = pd.DataFrame({"premium_1": rng.normal(size=20)}, index=INDEX)
    monkeypatch.setattr(fr, "_bars", lambda symbol: _bars())
    monkeypatch.setattr(fr, "_load", lambda kind, symbol: [])
    monkeypatch.setattr(fr, "kline_close", lambda rows: "INDEX-CLOSE")
    monkeypatch.setattr(fr, "check_grid", lambda index_close, close: {"med": 1.0})
    monkeypatch.setattr(fr, "funding_series", lambda rows: "SERIES")
    monkeypatch.setattr(fr, "funding_features", lambda s, p, idx: feats)
    monkeypatch.setattr(fr, "leaked_control", lambda f, index_close: "CTRL")
    monkeypatch.setattr(fr, "with_funding_features",
                        _with_features(ARM_DROP, CTRL_DROP))
    monkeypatch.setattr(fr, "build_features", lambda b, horizon_bars: own)
    oos, medians, corr = fr.collect("BTCUSDT")
    assert medians == {"med": 1.0} and isinstance(corr, float)
    rows = [i for i in IDS if i not in ARM_DROP | CTRL_DROP]
    spec = [("base", BASE_COLS, 0), ("funding", FULL_COLS, 1000),
            ("control", FULL_COLS, 2000)]
    return oos, rec.fits, spec, rows, TEST_YEARS_ALL, True


def _run_flow(monkeypatch):
    rec = _RecordingLgb()
    monkeypatch.setattr(ofr, "lgb", rec)
    own = _own()
    rng = np.random.default_rng(0)
    feats = pd.DataFrame({"flow_imbalance_1": rng.normal(size=20)}, index=INDEX)
    monkeypatch.setattr(ofr, "_bars", lambda symbol: _bars())
    monkeypatch.setattr(ofr, "_load_raw", lambda symbol: [])
    monkeypatch.setattr(ofr, "flow_frame", lambda raw: "FRAME")
    monkeypatch.setattr(ofr, "align_flow", lambda frame, bars: "FLOW")
    monkeypatch.setattr(ofr, "flow_features", lambda flow, close: feats)
    monkeypatch.setattr(ofr, "leaked_control", lambda f: "CTRL")
    monkeypatch.setattr(ofr, "with_flow_features", _with_features(ARM_DROP, CTRL_DROP))
    monkeypatch.setattr(ofr, "build_features", lambda b, horizon_bars: own)
    oos, corr = ofr.collect("BTCUSDT")
    assert isinstance(corr, float)
    rows = [i for i in IDS if i not in ARM_DROP | CTRL_DROP]
    spec = [("base", BASE_COLS, 0), ("flow", FULL_COLS, 1000),
            ("control", FULL_COLS, 2000)]
    return oos, rec.fits, spec, rows, TEST_YEARS_ALL, True


RUNNERS = {
    "archive._walk_forward": _run_walk_forward,
    "archive.collect/metrics": _run_archive("metrics"),
    "archive.collect/depth": _run_archive("depth"),
    "archive.collect/metrics/control-only": _run_archive("metrics", control_only=True),
    "cross_asset.collect": _run_cross,
    "funding.collect": _run_funding,
    "order_flow.collect": _run_flow,
}


@pytest.mark.parametrize("name", RUNNERS)
def test_fold_contract_purge_labels_columns_and_same_rows(monkeypatch, name):
    oos, fits, spec, rows, years, with_ret = RUNNERS[name](monkeypatch)
    _check(oos, fits, spec, rows, years, with_ret=with_ret)


def test_the_horizon_is_the_one_bar_the_purge_assumes():
    # the expected windows above are written for HORIZON = 1 (E7-E12, h = 1)
    assert lgr.HORIZON == 1
    assert car.HORIZON == fr.HORIZON == ofr.HORIZON == ar.HORIZON == 1
    assert lgr.TEST_YEARS == TEST_YEARS_ALL
    assert car.TEST_YEARS == fr.TEST_YEARS == ofr.TEST_YEARS == TEST_YEARS_ALL


@pytest.mark.parametrize("name", [n for n in RUNNERS if n != "archive._walk_forward"])
def test_no_arm_sees_a_row_the_other_arm_set_lacks(monkeypatch, name):
    """The same-rows contract in its own words: the row that only one of the
    arm / control matrices has (ids 5 and 10) is in no fit and no test fold of
    any arm. A union or either single index would let it in (or raise)."""
    oos, fits, spec, _, _, _ = RUNNERS[name](monkeypatch)
    lacking = ARM_DROP if name == "cross_asset.collect" else ARM_DROP | CTRL_DROP
    seen = {int(i) for ds in fits for i in ds.X["id"]}
    seen |= {int(i) for i in oos["base"]}
    assert not (seen & lacking)
    # within one test year every arm is fitted on the identical rows
    n = len(spec)
    for k in range(0, len(fits), n):
        assert len({tuple(ds.X.index) for ds in fits[k:k + n]}) == 1


# -------------------------------------------------------- archive collect()

def test_archive_row_share_is_arm_rows_in_test_years_over_base_rows_there(monkeypatch):
    # metrics test years 2023-2025 -> ids 8..19 = 12 base rows. The control
    # lacks id 10 (in 2023), the arm lacks id 5 (2022, outside the years).
    _, info = _run_archive_info(monkeypatch, "metrics")
    assert info["row_share"] == pytest.approx(11 / 12)
    # depth test years 2024-2025 -> ids 12..19 = 8 base rows, none dropped
    _, info = _run_archive_info(monkeypatch, "depth")
    assert info["row_share"] == pytest.approx(1.0)


def _run_archive_info(monkeypatch, arm):
    own = _own()
    monkeypatch.setattr(ar, "lgb", _RecordingLgb())
    monkeypatch.setattr(ar, "_perp_bars", lambda symbol: _bars())
    monkeypatch.setattr(ar, "_load", lambda dataset, symbol: "RAW")
    monkeypatch.setattr(ar, "check_stamp_convention",
                        lambda metrics, close: {"delay": 2})
    monkeypatch.setattr(ar, "metrics_features", lambda m, idx, delay: FEATS)
    monkeypatch.setattr(ar, "depth_features", lambda d, idx: FEATS)
    monkeypatch.setattr(ar, "leaked_control", lambda feats, close, col: "CTRL")
    monkeypatch.setattr(ar, "with_archive_features", _with_features(ARM_DROP, CTRL_DROP))
    monkeypatch.setattr(ar, "build_features", lambda bars, horizon_bars: own)
    return ar.collect("BTCUSDT", arm, False)


def test_archive_metrics_arm_checks_the_stamp_convention_and_passes_its_delay(monkeypatch):
    seen = {}
    own = _own()
    monkeypatch.setattr(ar, "lgb", _RecordingLgb())
    monkeypatch.setattr(ar, "_perp_bars", lambda symbol: _bars())
    monkeypatch.setattr(ar, "_load", lambda dataset, symbol: f"RAW-{dataset}")
    monkeypatch.setattr(ar, "check_stamp_convention",
                        lambda metrics, close: seen.setdefault("m", metrics) and
                        {"delay": 2, "corr_close_row": 0.1})
    monkeypatch.setattr(ar, "metrics_features",
                        lambda m, idx, delay: seen.update(delay=delay) or FEATS)
    monkeypatch.setattr(ar, "depth_features",
                        lambda d, idx: pytest.fail("depth features on the metrics arm"))
    monkeypatch.setattr(ar, "leaked_control", lambda feats, close, col: seen.update(col=col) or "CTRL")
    monkeypatch.setattr(ar, "with_archive_features", _with_features(ARM_DROP, CTRL_DROP))
    monkeypatch.setattr(ar, "build_features", lambda bars, horizon_bars: own)
    _, info = ar.collect("BTCUSDT", "metrics", False)
    assert seen["m"] == "RAW-metrics" and seen["delay"] == 2
    assert seen["col"] == "oi_change_16"
    assert info["delay"] == 2 and info["corr_close_row"] == 0.1


def test_archive_depth_arm_uses_depth_features_and_no_stamp_check(monkeypatch):
    own = _own()
    seen = {}
    monkeypatch.setattr(ar, "lgb", _RecordingLgb())
    monkeypatch.setattr(ar, "_perp_bars", lambda symbol: _bars())
    monkeypatch.setattr(ar, "_load", lambda dataset, symbol: f"RAW-{dataset}")
    monkeypatch.setattr(ar, "check_stamp_convention",
                        lambda m, c: pytest.fail("stamp check on the depth arm"))
    monkeypatch.setattr(ar, "metrics_features",
                        lambda m, idx, delay: pytest.fail("metrics features on depth"))
    monkeypatch.setattr(ar, "depth_features", lambda d, idx: seen.update(d=d) or FEATS)
    monkeypatch.setattr(ar, "leaked_control", lambda feats, close, col: seen.update(col=col) or "CTRL")
    monkeypatch.setattr(ar, "with_archive_features", _with_features(ARM_DROP, CTRL_DROP))
    monkeypatch.setattr(ar, "build_features", lambda bars, horizon_bars: own)
    _, info = ar.collect("BTCUSDT", "depth", False)
    assert seen == {"d": "RAW-bookDepth", "col": "depth_change_4"}
    assert "delay" not in info


def test_archive_control_only_scores_no_arm_column(monkeypatch):
    oos, _, _, _, _, _ = _run_archive("depth", control_only=True)(monkeypatch)
    assert set(oos.columns) == {"year", "y", "ret", "base", "control"}
    oos, _, _, _, _, _ = _run_archive("depth")(monkeypatch)
    assert set(oos.columns) == {"year", "y", "ret", "base", "control", "depth"}
