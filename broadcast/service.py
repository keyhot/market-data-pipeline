"""Broadcast manager runner (Sprint 14, A4): keep a YouTube broadcast alive and
public with nobody clicking "Go Live".

Shaped like scripts/stream_watchdog.py and director/service.py — a thin loop
over a pure decision (`broadcast.policy.tick`), with all I/O injected so every
behaviour above is unit-tested against a FakeYouTubeClient and never touches
the network.

Why it exists: OBS pushing to the ingest key proves nothing about whether the
stream is watchable. On 2026-07-27 it pushed for ~4h with no active broadcast
and nothing was public until a human opened the Live Control Room. This runner
is what makes that self-healing.

**Cardinal rule: never crash the stream.** A YouTube outage, an exhausted daily
quota, a revoked token — all log, back off, record nothing false, and leave OBS
running. The only fatal condition is a missing OAuth secret at boot, which is
fail-closed on purpose (exit 2, systemd restarts, it comes up when the secret
is there).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broadcast.policy import (  # noqa: E402
    BroadcastConfig,
    BroadcastState,
    select_broadcast,
    tick,
)

logger = logging.getLogger(__name__)


def broadcast_enabled() -> bool:
    """Opt-in like the director: unset counts as enabled for the systemd unit,
    `0`/`false`/`no` disables without a redeploy."""
    raw = os.environ.get("BROADCAST_ENABLED")
    return raw is None or raw.strip().lower() not in {"0", "false", "no"}


def config_from_env() -> BroadcastConfig:
    def _int(name, default):
        try:
            return int(os.environ.get(name, default))
        except ValueError:
            return default

    # Names match the ones A1 already documented in .env.example.
    return BroadcastConfig(
        grace_seconds=_int("BROADCAST_GRACE_SECONDS", 30),
        create_backoff_seconds=_int("BROADCAST_CREATE_BACKOFF_SECONDS", 120),
        stream_title=os.environ.get("BROADCAST_STREAM_TITLE", "Market World"),
        broadcast_title=os.environ.get("BROADCAST_TITLE", "Market World — live"),
        privacy=os.environ.get("BROADCAST_PRIVACY", "public"),
    )


def fetch_yt_state(client, state: BroadcastState, config: BroadcastConfig) -> dict:
    """One YouTube snapshot in the shape `tick` reasons about. Two reads per
    tick (list_broadcasts + find_stream) — well inside the free 10k/day quota
    at a 30s cadence."""
    stream = client.find_stream(config.stream_title)
    if stream is None:
        # Loudly, every tick: a wrong BROADCAST_STREAM_TITLE is the likeliest
        # setup mistake and its natural symptom is total silence — no stream
        # means `_stream_active` is False, so `tick` never asks for anything
        # and no error is ever raised. Name what we looked for.
        logger.warning(
            "No YouTube liveStream titled %r — check BROADCAST_STREAM_TITLE "
            "against YouTube Studio → Go Live → Stream settings. The manager "
            "cannot bind or go live until it matches.",
            config.stream_title,
        )
    # Pass the id we know: an exact lookup can't be crowded out by the
    # channel's completed-broadcast history the way a scan can.
    broadcasts = client.list_broadcasts(broadcast_id=state.current_broadcast_id)
    if state.current_broadcast_id is not None and any(
        b.get("lifecycle") in ("ready", "testing") for b in broadcasts
    ):
        # KI-086: while ours waits, the exact lookup can never see a duplicate
        # already live on our key — the one thing that keeps ours waiting
        # forever. Scan too. Waiting is normally a tick or two, so the quota
        # cost is bounded; the steady (live) state stays a single lookup.
        broadcasts = broadcasts + client.list_broadcasts()
    return {
        "broadcast": select_broadcast(
            broadcasts,
            state.current_broadcast_id,
            stream_id=stream["id"] if stream else None,
        ),
        "stream": stream,
    }


def apply(actions, state: BroadcastState, client, record_event, config, now) -> None:
    """Perform the decided actions — the only place with side effects.

    Each action is guarded individually: a failing create must not stop a
    pending `record` from reaching the world log, because that log is what the
    soak report's public-uptime number is computed from.
    """
    from broadcast.events import build_from_action

    events = []
    for action in actions:
        try:
            if action[0] == "create_and_bind":
                _create_and_bind(client, state, config)
            elif action[0] == "transition_live":
                _transition_live(client, state, action[1])
            elif action[0] == "record":
                events.append(build_from_action(action[1], action[2], occurred_at=now))
        except Exception as exc:
            # Quota exhaustion, a revoked token, an API 5xx: log it, keep the
            # stream up, try again after the backoff.
            logger.warning(
                "broadcast_action_failed",
                extra={"action": action[0], "error": str(exc)},
                exc_info=True,
            )
    # Always call the recorder, even with no events, so it drains the spool
    # backlog every tick (flush-on-tick, same as the director).
    if record_event is not None:
        record_event(events)


# ~30 minutes of refusals at the 30 s cadence. YouTube refuses every
# transition while a fresh broadcast settles, and auto-start — not our call —
# is what normally takes it live: 51 refusals on 2026-08-18 and on 2026-09-15
# were both healthy go-lives. The threshold sits above that benign ceiling.
REFUSED_TRANSITION_ALERT_AT = 60


def _transition_live(client, state: BroadcastState, broadcast_id: str) -> None:
    """Ask YouTube to go live, logging a refusal streak once, not per tick
    (KI-086: ~16k identical tracebacks buried the one fact that mattered)."""
    try:
        client.transition(broadcast_id, "live")
    except Exception as exc:
        state.refused_transitions += 1
        n = state.refused_transitions
        if n == 1:
            logger.warning(
                "broadcast_action_failed",
                extra={"action": "transition_live", "error": str(exc)},
                exc_info=True,
            )
        elif n == REFUSED_TRANSITION_ALERT_AT:
            logger.error(
                "YouTube has refused %d consecutive transitions of broadcast %s "
                "to live — the public stream is probably not ours or not up. "
                "Is another broadcast live on our stream key? (KI-086)",
                n,
                broadcast_id,
            )
        return
    if state.refused_transitions:
        logger.info(
            "Transition accepted after %d refusals", state.refused_transitions
        )
    state.refused_transitions = 0
    logger.info("Transitioned broadcast live", extra={"id": broadcast_id})


def _create_and_bind(client, state: BroadcastState, config: BroadcastConfig) -> None:
    stream = client.find_stream(config.stream_title)
    if stream is None:
        # Without the persistent ingest stream there is nothing to bind to, and
        # an unbound broadcast can never go live — better to wait than to
        # create orphans on every backoff window.
        raise RuntimeError(f"no liveStream titled {config.stream_title!r}")
    created = client.insert_broadcast(config.broadcast_title, config.privacy)
    client.bind_broadcast(created["id"], stream["id"])
    # The runner owns this: `tick` learns the id from the *next* snapshot, and
    # until then `select_broadcast` must not re-adopt the old broadcast and
    # create another one on the next backoff window.
    state.current_broadcast_id = created["id"]
    logger.info(
        "Created and bound broadcast",
        extra={"broadcast_id": created["id"], "stream_id": stream["id"]},
    )


def tick_once(yt_state, obs_streaming, state, config, now, apply_actions) -> None:
    """One decision + application, sharing the state the runner owns."""
    new_state, actions = tick(yt_state, obs_streaming, state, config, now)
    _adopt(state, new_state)
    # A tick that asks for no transition ends the streak — the broadcast went
    # live (usually by auto-start, since our own call is almost always the one
    # refused), or there is nothing to transition. Resetting only on our call
    # succeeding would leave the count stuck after every healthy go-live.
    if not any(action[0] == "transition_live" for action in actions):
        state.refused_transitions = 0
    # Unconditionally, even with no actions: a healthy live broadcast is quiet
    # on essentially every tick, and `apply` is what drains the event spool. A
    # `if actions:` guard here would strand a spooled broadcast_live until the
    # next action — possibly the broadcast's end, days later — and the uptime
    # report would say "not measured" while the stream was public.
    apply_actions(actions, state, now)


def _adopt(state: BroadcastState, new_state: BroadcastState) -> None:
    """Copy tick's decisions into the runner's long-lived state, *keeping* a
    broadcast id the runner just created (tick only ever saw the previous
    snapshot, so its id is one tick stale)."""
    state.current_lifecycle = new_state.current_lifecycle
    state.last_create_at = new_state.last_create_at
    state.stream_active_since = new_state.stream_active_since
    if new_state.current_broadcast_id is not None:
        state.current_broadcast_id = new_state.current_broadcast_id


def metadata_enabled() -> bool:
    """Opt-IN, unlike the manager itself: this rewrites the public title and
    description, so it changes nothing until a human sets
    `BROADCAST_METADATA_ENABLED=1`."""
    raw = os.environ.get("BROADCAST_METADATA_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes"}


class MetadataRefresher:
    """Keeps the live broadcast's title/description/tags true (Sprint 17 #27).

    Writes on a new broadcast id at once, then at most every `interval_seconds`
    and only when the text changed: `videos.update` costs 50 quota units from
    the same 10k/day the go-live transitions need, and a quota-starved
    transition is a stream that isn't public. Never raises; a failed write is
    retried on the next tick.
    """

    def __init__(self, client, read_record, interval_seconds: float = 21600):
        self._client = client
        self._read_record = read_record
        self._interval = interval_seconds
        self._written_id = None
        self._written_at = None
        self._written = None

    def maybe_refresh(self, broadcast_id, now) -> None:
        if broadcast_id is None:
            return
        fresh = broadcast_id != self._written_id
        if not fresh and (now - self._written_at).total_seconds() < self._interval:
            return
        try:
            from broadcast.metadata import build_snippet

            record = self._read_record()
            current = self._client.get_video_snippet(broadcast_id)
            if current is None:
                return
            snippet = build_snippet(record, current)
            if not fresh and snippet == self._written:
                self._written_at = now
                return
            self._client.update_video_snippet(broadcast_id, snippet)
        except Exception as exc:
            logger.warning(
                "broadcast_metadata_failed", extra={"error": str(exc)}, exc_info=True
            )
            return
        self._written_id, self._written_at, self._written = broadcast_id, now, snippet
        logger.info("Broadcast metadata written", extra={"title": snippet["title"]})


def write_status(**status) -> None:
    """Publish this tick's status for the ops alerter (KI-086's alert half:
    the ERROR log line is the channel that read green for six days). Written
    atomically; never raises — nothing about alerting may stop the manager."""
    path = Path(os.environ.get("BROADCAST_STATUS_PATH", "data/broadcast_status.json"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status))
        tmp.replace(path)
    except Exception as exc:
        logger.warning("broadcast status not written", extra={"error": str(exc)})


def run(
    fetch_yt_state,
    obs_probe,
    apply_actions,
    config=None,
    sleep_seconds=30.0,
    max_ticks=None,
    on_status=write_status,
) -> None:
    """Poll YouTube + OBS, decide, apply. Loops forever unless `max_ticks` is
    given (tests). Every tick body is guarded — the loop is the thing that must
    survive, since an unattended 24/7 stream has nobody to restart it."""
    config = config or BroadcastConfig()
    state = BroadcastState(None, None, None, None)
    ticks = 0
    tick_failures = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        now = datetime.now(timezone.utc)
        try:
            obs_streaming = obs_probe()
            # The fetch needs the runner's state: which broadcast is *ours*
            # decides which one `select_broadcast` returns.
            yt_state = fetch_yt_state(state)
            tick_once(yt_state, obs_streaming, state, config, now, apply_actions)
            tick_failures = 0
        except Exception as exc:
            tick_failures += 1
            logger.warning(
                "broadcast_tick_failed", extra={"error": str(exc)}, exc_info=True
            )
        on_status(
            updated_at=now.isoformat(),
            broadcast_id=state.current_broadcast_id,
            lifecycle=state.current_lifecycle,
            refused_transitions=state.refused_transitions,
            consecutive_tick_failures=tick_failures,
        )
        if sleep_seconds:
            time.sleep(sleep_seconds)


def main() -> int:
    from config.logging import init_logging

    init_logging()
    if not broadcast_enabled():
        logger.info("Broadcast manager disabled (BROADCAST_ENABLED); exiting")
        return 0

    from broadcast.youtube_client import MissingOAuth, YouTubeLiveClient

    try:
        client = YouTubeLiveClient()
    except MissingOAuth as exc:
        # Fail-closed, mirroring the freqtrade secrets gate and the director's
        # OBS exit: never boot half-configured, let systemd retry.
        logger.error("Broadcast manager has no OAuth credentials: %s", exc)
        return 2

    from broadcast.events import record_broadcast_events
    from scripts import stream_ctl

    config = config_from_env()
    state_holder = {"obs": None}

    def obs_probe() -> bool:
        """OBS is allowed to be down (a watchdog restart is routine) — that's
        `not streaming`, not an error."""
        try:
            if state_holder["obs"] is None:
                state_holder["obs"] = stream_ctl.make_client()
            return bool(stream_ctl.get_status(state_holder["obs"])["streaming"])
        except Exception:
            state_holder["obs"] = None  # force a reconnect next tick
            return False

    refresher = None
    if metadata_enabled():
        from storage.postgres_store import get_signal_record

        refresher = MetadataRefresher(
            client,
            get_signal_record,
            float(os.environ.get("BROADCAST_METADATA_REFRESH_SECONDS", 21600)),
        )

    def apply_actions(actions, state, now):
        apply(actions, state, client, record_broadcast_events, config, now)
        if refresher is not None:
            refresher.maybe_refresh(state.current_broadcast_id, now)

    run(
        fetch_yt_state=lambda state: fetch_yt_state(client, state, config),
        obs_probe=obs_probe,
        apply_actions=apply_actions,
        config=config,
        sleep_seconds=float(os.environ.get("BROADCAST_POLL_SECONDS", 30)),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
