"""KI-046: the stream can be dark while every light is green.

OBS reported `streaming: true` at a 0.007% drop ratio while the frame was
white, and the watchdog's content probe only reads Postgres connectivity. So
the page itself has to say it is alive - and say it in a way that a FROZEN
page cannot fake.
"""
from world.renderer_health import record_beat, renderer_status


def test_a_fresh_beat_is_healthy():
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    status = renderer_status(store, now=1002.0, started_at=0.0)
    assert status["healthy"] is True
    assert status["pages"]["127.0.0.5:8000"]["age_seconds"] == 2.0


def test_a_silent_page_goes_unhealthy_once_it_is_stale():
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    assert renderer_status(store, now=1060.0, started_at=0.0)["healthy"] is False


def test_a_page_whose_frames_stop_advancing_is_frozen_not_healthy():
    """The whole point of carrying a frame count: a page whose timer still
    fires but whose render loop has stopped is exactly the white-frame case,
    and it would keep posting a perfectly fresh heartbeat forever."""
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1010.0)
    status = renderer_status(store, now=1011.0, started_at=0.0)
    assert status["healthy"] is False
    assert status["pages"]["127.0.0.5:8000"]["frozen"] is True


def test_advancing_frames_are_not_frozen():
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    record_beat(store, host="127.0.0.5:8000", page="world", frames=760, now=1010.0)
    assert renderer_status(store, now=1011.0, started_at=0.0)["healthy"] is True


def test_no_beat_at_all_is_healthy_inside_the_grace_period():
    """An API restart empties the registry. Reporting 'blank' for the seconds
    before OBS's page posts again would make every deploy look like an outage -
    which is how KI-038 put a false row in the append-only log."""
    store = {}
    assert renderer_status(store, now=30.0, started_at=0.0)["healthy"] is True
    assert renderer_status(store, now=300.0, started_at=0.0)["healthy"] is False


def test_a_page_that_stopped_posting_long_ago_is_forgotten_not_held_against_us():
    """A dev tab closed ten minutes ago must not pin the fleet unhealthy
    forever - that would record stream_dropped on a healthy stream, the same
    class of false row as KI-038."""
    store = {}
    record_beat(store, host="localhost:8000", page="world", frames=10, now=1000.0)
    record_beat(store, host="127.0.0.5:8000", page="world", frames=10, now=2000.0)
    record_beat(store, host="127.0.0.5:8000", page="world", frames=99, now=2010.0)
    status = renderer_status(store, now=2011.0, started_at=0.0)
    assert "localhost:8000" not in status["pages"]
    assert status["healthy"] is True


def test_a_second_page_cannot_cover_for_a_dead_one():
    """A dev tab on localhost must not mask a dead OBS source. Beats are keyed
    by Host, and the watchdog names the host it cares about."""
    store = {}
    record_beat(store, host="localhost:8000", page="world", frames=999, now=1000.0)
    status = renderer_status(
        store, now=1001.0, started_at=0.0, required_host="127.0.0.5:8000"
    )
    assert status["healthy"] is False


def test_a_page_that_reloaded_is_not_frozen_just_because_its_counter_reset():
    """A browser-source refresh restarts the render loop at a low frame
    count. Reading that as a stall would record an outage on the page we
    just successfully recovered - a false row in an append-only log."""
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=500, now=1000.0)
    record_beat(store, host="127.0.0.5:8000", page="world", frames=5, now=1010.0)
    status = renderer_status(store, now=1011.0, started_at=0.0)
    assert status["pages"]["127.0.0.5:8000"]["frozen"] is False
    assert status["healthy"] is True


def test_age_exactly_at_stale_threshold_is_healthy():
    """Boundary test: age == STALE_AFTER (45.0) should still be healthy."""
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    status = renderer_status(store, now=1045.0, started_at=0.0)
    assert status["healthy"] is True
    assert status["pages"]["127.0.0.5:8000"]["age_seconds"] == 45.0


def test_span_exactly_at_frozen_threshold_with_unchanged_frames_is_frozen():
    """Boundary: span == FROZEN_AFTER (8.0) with unchanged frames is frozen."""
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1008.0)
    status = renderer_status(store, now=1009.0, started_at=0.0)
    assert status["pages"]["127.0.0.5:8000"]["frozen"] is True


def test_age_exactly_at_prune_threshold_is_not_yet_pruned():
    """Boundary test: age == PRUNE_AFTER (600.0) should not yet be pruned."""
    store = {}
    record_beat(store, host="127.0.0.5:8000", page="world", frames=100, now=1000.0)
    status = renderer_status(store, now=1600.0, started_at=0.0)
    assert "127.0.0.5:8000" in status["pages"]
