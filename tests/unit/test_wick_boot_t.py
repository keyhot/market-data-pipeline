"""R3-b Amendment 1: the day-clustered studentized (bootstrap-t) test.

Vault ``Docs/r3b-preregistration.md`` §Amendment 1. Stage A's S2 placebo showed
the percentile bound over-rejecting (0.087 > 0.08) on ~250 days. These tests pin
the replacement: its formula (per-resample SE at the resample's own mean), its
size on a clustered, skewed null where the percentile bound is mis-sized, and
that the real scorer and the synthetic harness share the one implementation.
"""
import numpy as np
import pytest

from model import wick_catch as wc


def _cr1_se(s, n, m):
    g = len(s)
    return np.sqrt(g / (g - 1) * np.sum((s - m * n) ** 2)) / np.sum(n)


def test_boot_t_matches_hand_computation():
    s = np.array([3.0, -1.0, 4.0, 0.5, -2.0])
    n = np.array([2.0, 1.0, 3.0, 1.0, 2.0])
    idx = np.array([[0, 1, 2, 3, 4], [0, 0, 2, 2, 4], [1, 3, 3, 4, 0], [2, 2, 2, 1, 0]])
    r = wc.boot_t_bounds(s, n, idx, alpha=0.05)
    m = s.sum() / n.sum()
    se = _cr1_se(s, n, m)
    t = []
    for row in idx:
        mb = s[row].sum() / n[row].sum()
        t.append((mb - m) / _cr1_se(s[row], n[row], mb))   # resample's OWN mean
    t = np.asarray(t)
    assert r["mean"] == pytest.approx(m)
    assert r["se"] == pytest.approx(se)
    assert r["lb"] == pytest.approx(m - np.quantile(t, 0.95) * se)
    assert r["ci95"][0] == pytest.approx(m - np.quantile(t, 0.975) * se)
    assert r["ci95"][1] == pytest.approx(m - np.quantile(t, 0.025) * se)


def test_se_at_mhat_mutant_is_caught():
    s = np.array([3.0, -1.0, 4.0, 0.5, -2.0])
    n = np.array([2.0, 1.0, 3.0, 1.0, 2.0])
    idx = np.array([[0, 0, 2, 2, 4], [1, 3, 3, 4, 0], [2, 2, 2, 1, 0], [4, 4, 1, 1, 3]])
    good = wc.boot_t_bounds(s, n, idx)
    bad = wc.boot_t_bounds(s, n, idx, _se_at_mhat=True)
    assert bad["lb"] != pytest.approx(good["lb"])


def test_degenerate_resamples_are_dropped_and_counted():
    s = np.array([1.0, 2.0, -1.0])
    n = np.array([1.0, 1.0, 1.0])
    idx = np.array([[0, 0, 0], [0, 1, 2], [2, 1, 1]])           # first resample has se* = 0
    r = wc.boot_t_bounds(s, n, idx)
    assert r["degenerate"] == 1 and np.isfinite(r["lb"])


def _skewed_null_rate(method, reps=1000, g=250, n_boot=399, seed=7):
    """Left-skewed (negated, centred lognormal) day effects, 1-4 events/day, true mean 0."""
    rng = np.random.default_rng(seed)
    rej = 0
    for _ in range(reps):
        n = rng.integers(1, 5, g)
        d = -(rng.lognormal(0.0, 1.0, g) - np.exp(0.5))
        day = np.repeat(np.arange(g), n)
        x = d[day] + 0.5 * rng.standard_normal(len(day))
        r = wc.cluster_bootstrap(x, np.zeros_like(x), day, n_boot=n_boot, rng=rng, method=method)
        rej += r["lb_primary"] > 0
    return rej / reps


def test_bootstrap_t_is_size_correct_where_percentile_is_not():
    pct = _skewed_null_rate("percentile")
    bt = _skewed_null_rate("t")
    assert pct > 0.08                       # the stage-A failure mode, reproduced
    assert 0.02 <= bt <= 0.08               # the amended test lands inside the S2 band


def test_cluster_bootstrap_defaults_to_bootstrap_t():
    rng = np.random.default_rng(1)
    x = rng.normal(50, 10, 400)
    c = rng.normal(0, 10, 400)
    day = np.repeat(np.arange(100), 4)
    r = wc.cluster_bootstrap(x, c, day, n_boot=999, rng=np.random.default_rng(2))
    assert r["method"] == "t"
    s = np.bincount(day, weights=x)
    n = np.bincount(day).astype(float)
    idx = np.random.default_rng(2).integers(0, 100, (999, 100))
    assert r["lb_primary"] == pytest.approx(wc.boot_t_bounds(s, n, idx)["lb"])


def test_power_harness_uses_the_shared_bootstrap_t(monkeypatch):
    calls = []
    real = wc.boot_t_bounds

    def spy(*a, **k):
        calls.append(k.get("method", "t"))
        return real(*a, **k)

    monkeypatch.setattr(wc, "boot_t_bounds", spy)
    days = np.array([[1, 1, 1], [0, 1, 0], [2, 0, 0]] * 4)
    wc.power(0, days, n_days=50, n_seeds=3, n_boot=99, seed=1)
    assert len(calls) == 6 and set(calls) == {"t"}           # both legs, every seed
