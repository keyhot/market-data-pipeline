from api.metrics import MetricsRegistry


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_snapshot_empty_registry():
    clock = FakeClock()
    registry = MetricsRegistry(clock=clock)
    clock.now += 5

    snapshot = registry.snapshot()

    assert snapshot["uptime_seconds"] == 5.0
    assert snapshot["total_requests"] == 0
    assert snapshot["routes"] == {}


def test_record_aggregates_count_latency_and_statuses():
    registry = MetricsRegistry(clock=FakeClock())

    registry.record("GET", "/health", 200, 0.010)
    registry.record("GET", "/health", 200, 0.030)
    registry.record("GET", "/health", 404, 0.020)

    snapshot = registry.snapshot()
    route = snapshot["routes"]["GET /health"]

    assert snapshot["total_requests"] == 3
    assert route["count"] == 3
    assert route["avg_ms"] == 20.0
    assert route["max_ms"] == 30.0
    assert route["statuses"] == {"200": 2, "404": 1}


def test_routes_are_tracked_separately():
    registry = MetricsRegistry(clock=FakeClock())

    registry.record("GET", "/health", 200, 0.01)
    registry.record("GET", "/ticker/{ticker_symbol}/{time_range}", 200, 0.05)

    routes = registry.snapshot()["routes"]

    assert routes["GET /health"]["count"] == 1
    assert routes["GET /ticker/{ticker_symbol}/{time_range}"]["count"] == 1


def test_reset_clears_routes():
    registry = MetricsRegistry(clock=FakeClock())
    registry.record("GET", "/health", 200, 0.01)

    registry.reset()

    snapshot = registry.snapshot()
    assert snapshot["total_requests"] == 0
    assert snapshot["routes"] == {}
