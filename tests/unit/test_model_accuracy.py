"""KI-012: the /world room showed the model at ~49.8% over an event-window fold
while the overlay strips showed per-symbol last-50 (BTC 56% / ETH 50%) — three
disagreeing numbers. get_model_accuracy is the one honest basis: the last N
resolved signals PER SYMBOL, summed, matching the strip's get_signal_accuracy."""

import storage.postgres_store as ps


def test_model_accuracy_is_per_symbol_sum_at_the_strip_window(monkeypatch):
    fake = {"BTCUSDT": {"resolved": 50, "wins": 28, "hit_rate": 0.56},
            "ETHUSDT": {"resolved": 50, "wins": 25, "hit_rate": 0.50}}
    monkeypatch.setattr(ps, "get_signal_accuracy",
                        lambda s, interval="1m", window=50: fake[s])
    acc = ps.get_model_accuracy(["BTCUSDT", "ETHUSDT"])   # default window
    assert acc["window"] == 50          # must match the strip's default (KI-012)
    assert acc["resolved"] == 100
    assert acc["wins"] == 53 and acc["losses"] == 47
    assert acc["hit_rate"] == 0.53
    assert set(acc["per_symbol"]) == {"BTCUSDT", "ETHUSDT"}


def test_model_accuracy_empty_when_nothing_resolved(monkeypatch):
    monkeypatch.setattr(ps, "get_signal_accuracy",
                        lambda s, interval="1m", window=100: {"resolved": 0, "wins": 0,
                                                              "hit_rate": None})
    acc = ps.get_model_accuracy(["BTCUSDT"], window=100)
    assert acc["resolved"] == 0 and acc["hit_rate"] is None
