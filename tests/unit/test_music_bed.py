"""The runner's seam to OBS, with a fake client — the settings it sends are the
difference between a music bed and one track on repeat."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from music.library import Track
from music.player import BedState, MediaStatus
from scripts import music_bed

T0 = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    """Records what the runner asked OBS to do."""

    def __init__(self, status=None, raise_status=False):
        self._status = status or FakeResp(
            media_state="OBS_MEDIA_STATE_NONE", media_cursor=0, media_duration=0
        )
        self._raise_status = raise_status
        self.settings = []
        self.volumes = []
        self.monitors = []

    def get_media_input_status(self, name):
        if self._raise_status:
            raise RuntimeError("no such input")
        return self._status

    def set_input_settings(self, name, settings, overlay):
        self.settings.append((name, settings, overlay))

    def set_input_volume(self, name, vol_db=None):
        self.volumes.append((name, vol_db))

    def set_input_audio_monitor_type(self, name, kind):
        self.monitors.append((name, kind))


def _track(name="alpha"):
    return Track(
        file=f"{name}.mp3", title=name.title(), artist="Test Artist", source="Mixkit",
        source_url="https://mixkit.co/free-stock-music/",
        license="Mixkit Stock Music Free License",
        license_url="https://mixkit.co/license/#musicFree", duration_seconds=90.0,
    )


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """publish() must never be the reason the music stops."""
    monkeypatch.setattr(music_bed, "publish", lambda *a, **k: None)


def test_a_cold_bed_starts_a_track():
    client = FakeClient()
    state = music_bed.run_once(client, BedState(), [_track()], Path("/music"), T0)
    assert state.current == "alpha.mp3"
    name, settings, overlay = client.settings[0]
    assert name == "audio-bed"
    assert settings["local_file"] == "/music/alpha.mp3"
    assert overlay is True


def test_the_settings_the_runner_sends_make_it_a_bed_not_a_loop():
    client = FakeClient()
    music_bed.run_once(client, BedState(), [_track()], Path("/music"), T0)
    _, settings, _ = client.settings[0]
    assert settings["looping"] is False, "a looping track makes every later credit false"
    assert settings["restart_on_activate"] is False, "would restart on every scene switch"
    assert settings["clear_on_media_end"] is True, "how the player learns a track ended"


def test_the_bed_is_audible_on_the_broadcast_not_just_at_the_desk():
    """MONITOR_ONLY plays locally and sends nothing to the stream, which looks
    exactly like success from the operator's chair."""
    client = FakeClient()
    music_bed.run_once(client, BedState(), [_track()], Path("/music"), T0)
    assert client.monitors == [("audio-bed", "OBS_MONITORING_TYPE_NONE")]
    assert client.volumes == [("audio-bed", music_bed.DEFAULT_GAIN_DB)]


def test_a_playing_track_is_not_restarted():
    playing = FakeResp(
        media_state="OBS_MEDIA_STATE_PLAYING", media_cursor=5000, media_duration=90000
    )
    client = FakeClient(status=playing)
    state = music_bed.run_once(client, BedState(), [_track()], Path("/music"), T0)
    client.settings.clear()
    music_bed.run_once(client, state, [_track()], Path("/music"), T0)
    assert client.settings == [], "re-sending local_file would restart the track"


def test_an_unreadable_status_is_not_playing_rather_than_a_crash():
    client = FakeClient(raise_status=True)
    assert music_bed.read_status(client).state == "OBS_MEDIA_STATE_NONE"


def test_a_status_with_null_fields_does_not_crash_the_tick():
    """A source that has never been handed a file answers with nulls."""
    client = FakeClient(
        status=FakeResp(media_state=None, media_cursor=None, media_duration=None)
    )
    status = music_bed.read_status(client)
    assert status.state == "OBS_MEDIA_STATE_NONE"
    state = music_bed.run_once(client, BedState(), [_track()], Path("/music"), T0)
    assert state.current == "alpha.mp3"


def test_a_mixer_failure_does_not_stop_the_music(monkeypatch):
    client = FakeClient()

    def boom(*a, **k):
        raise RuntimeError("mixer gone")

    monkeypatch.setattr(client, "set_input_audio_monitor_type", boom)
    state = music_bed.run_once(client, BedState(), [_track()], Path("/music"), T0)
    assert state.current == "alpha.mp3"
    assert client.settings, "the file was still set"


def test_a_publish_failure_is_swallowed(monkeypatch):
    """A Postgres blip should cost the credit line, not the music. `publish`
    reaches the DB itself, so this asserts on the real function, not the stub."""
    import storage.postgres_store as store

    def boom(**kwargs):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(store, "set_now_playing", boom)
    music_bed.publish(_track(), T0)  # must not raise
