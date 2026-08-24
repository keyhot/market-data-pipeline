"""Music bed runner (Sprint 11 close-out): plays the licensed bed and publishes
what is playing, so the overlay can credit it.

Shape follows `stream_watchdog.py`: the decision is a pure `tick()` in
`music/player.py`, and this file owns the three things that cannot be tested —
the OBS socket, the clock, and Postgres.

Why the runner drives track-by-track instead of handing OBS a folder: no OBS
source kind reports *which* file it is playing. A playlist source would make
the bed trivial and the credit impossible. Playing one file at a time costs one
websocket call every ~90 seconds and makes now-playing exact.

Run it as a systemd --user unit next to the director and the watchdog; see the
streaming runbook in the vault.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from music import library  # noqa: E402
from music.player import BedState, MediaStatus, tick  # noqa: E402

logger = logging.getLogger(__name__)

BED_INPUT = "audio-bed"
POLL_SECONDS = 5.0

# The resting level of the bed. Shares the tier-0 value with
# `stream_scene.audio_gain_db` so the (still unwired) salience swell starts from
# exactly the level the bed actually plays at.
DEFAULT_GAIN_DB = -18.0


def _obs_client():
    """Late import so the module is importable — and testable — without OBS."""
    from scripts.stream_ctl import make_client

    return make_client()


def read_status(client, input_name: str = BED_INPUT) -> MediaStatus:
    """What OBS says about the bed. A source that has never been handed a file
    answers with nulls or errors; both mean 'not playing', not 'broken'."""
    try:
        r = client.get_media_input_status(input_name)
    except Exception as exc:  # noqa: BLE001 - any request error means "unknown"
        logger.debug("media status unavailable", extra={"error": str(exc)})
        return MediaStatus()
    return MediaStatus(
        state=getattr(r, "media_state", None) or "OBS_MEDIA_STATE_NONE",
        cursor_ms=getattr(r, "media_cursor", None),
        duration_ms=getattr(r, "media_duration", None),
    )


def play(client, track, directory: Path, gain_db: float = DEFAULT_GAIN_DB) -> None:
    """Point the bed input at one file and start it.

    `set_input_settings` on `local_file` restarts playback by itself, so there
    is no separate play call. The other three settings are the ones that decide
    whether a *bed* behaves like a bed:

    - `looping: False` — the runner advances tracks; a looping source would
      play one track forever and make every credit after the first a lie.
    - `restart_on_activate: False` — OBS's default is True, which restarts the
      file every time its scene becomes active. The director switches scenes on
      salience, so the default would restart the music on every switch.
    - `clear_on_media_end: True` — leaves the source in ENDED, which is how
      `tick()` learns the track is over.
    """
    client.set_input_settings(
        BED_INPUT,
        {
            "local_file": str(directory / track.file),
            "is_local_file": True,
            "looping": False,
            "restart_on_activate": False,
            "clear_on_media_end": True,
        },
        True,
    )
    # Audible on the broadcast, not just at the desk: MONITOR_ONLY plays out of
    # the operator's speakers and sends nothing to the stream, which looks
    # exactly like success from here.
    try:
        client.set_input_audio_monitor_type(BED_INPUT, "OBS_MONITORING_TYPE_NONE")
        client.set_input_volume(BED_INPUT, vol_db=gain_db)
    except Exception as exc:  # noqa: BLE001 - mixer tweaks must not stop playback
        logger.warning("could not set bed audio levels", extra={"error": str(exc)})


def publish(track, started_at: datetime) -> None:
    """Record the current track for the overlay. Never fatal: a Postgres blip
    should cost the credit line, not the music."""
    try:
        from storage import postgres_store

        postgres_store.set_now_playing(
            track_file=track.file,
            title=track.title,
            artist=track.artist,
            source=track.source,
            source_url=track.source_url,
            license_name=track.license,
            duration_seconds=track.duration_seconds or None,
            started_at=started_at,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not publish now-playing",
            extra={"track": track.file, "error": str(exc)},
        )


def run_once(client, state: BedState, tracks: list, directory: Path, now: datetime):
    """One observation: ask OBS, decide, act. Returns the new state."""
    state, action = tick(state, read_status(client), now, tracks)
    if action.is_change:
        play(client, action.play, directory)
        publish(action.play, now)
        logger.info(
            "Bed advanced",
            extra={
                "track": action.play.file,
                "credit": action.play.credit,
                "reason": action.reason,
            },
        )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Play and credit the stream music bed."
    )
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    parser.add_argument(
        "--once", action="store_true", help="single tick, for smoke-testing the seam"
    )
    args = parser.parse_args()

    init_logging()
    directory = library.audio_dir()
    if directory is None:
        logger.error("STREAM_AUDIO_DIR is not set — nothing to play")
        raise SystemExit(2)

    tracks = library.playable_tracks(directory=directory)
    if not tracks:
        logger.error("no manifest track is present in %s", directory)
        raise SystemExit(2)

    total = sum(t.duration_seconds for t in tracks) / 60
    logger.info(
        "Music bed started",
        extra={"tracks": len(tracks), "minutes": round(total), "dir": str(directory)},
    )

    client = _obs_client()
    # Seeded from the wall clock so a restart does not replay the same order;
    # `tick` stays deterministic because the seed lives in the state.
    state = BedState(seed=int(time.time()))
    while True:
        state = run_once(client, state, tracks, directory, datetime.now(timezone.utc))
        if args.once:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
