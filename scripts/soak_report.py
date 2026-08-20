"""Soak-test report (Sprint 11): summarize stream uptime from stream_* world
events as markdown. Failures found by the soak are the SUCCESS output of the
test — same truth-over-vanity rule as the backtest.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Runnable as a plain script (KI-007): `python scripts/soak_report.py` puts
# scripts/ — not the repo root — on sys.path, so `storage` won't import without
# this. Mirrors scripts/stream_ctl.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from world.state import is_degraded_stream_event  # noqa: E402


def compute_uptime(
    events: list[dict], window_start: datetime, window_end: datetime
) -> dict:
    """Fold stream_* events (any order) into uptime numbers. Outages open on
    stream_dropped (except reason=dropped_frames — degraded, not down) and
    close on the next stream_started."""
    ordered = sorted(events, key=lambda e: e["occurred_at"])
    outages: list[dict] = []
    open_outage: dict | None = None
    for event in ordered:
        occurred = datetime.fromisoformat(event["occurred_at"])
        etype = event["event_type"]
        payload = event.get("payload") or {}
        if etype in ("stream_dropped", "stream_stopped", "stream_reconnected"):
            # A stop or a non-degraded drop opens downtime until the next start.
            # A degraded event is the one "down" event that isn't downtime — the
            # stream stayed live but impaired. Since KI-008 the watchdog records
            # unexpected inactivity as stream_stopped, so it lands here too and
            # is counted. The rule itself is NOT restated here: it lives in
            # world.state.is_degraded_stream_event, which world/state.py's
            # downtime accrual also calls. Two hand-kept copies of it is how
            # KI-019 happened, and KI-021 would have made it three.
            if is_degraded_stream_event(etype, payload):
                # Degraded = live-but-impaired, zero downtime. Record and move
                # on — leaving it sitting in open_outage would block a real
                # outage that fires before the next stream_started, deleting
                # that outage's downtime from the report.
                outages.append(
                    {
                        "start": occurred,
                        "end": occurred,
                        "reason": payload.get("reason")
                        or ("rtmp_reconnect" if etype == "stream_reconnected"
                            else "degraded"),
                    }
                )
            elif open_outage is None:
                open_outage = {
                    "start": occurred,
                    "end": None,
                    "reason": payload.get("reason", "unknown"),
                }
        elif etype == "stream_started" and open_outage is not None:
            if open_outage["end"] is None:
                open_outage["end"] = occurred
            outages.append(open_outage)
            open_outage = None
    if open_outage is not None:
        if open_outage["end"] is None:
            open_outage["end"] = window_end
        outages.append(open_outage)

    downtime = sum((o["end"] - o["start"]).total_seconds() for o in outages)
    window = (window_end - window_start).total_seconds()
    uptime_pct = round(100.0 * (1 - downtime / window), 2) if window else 100.0
    return {
        "uptime_pct": uptime_pct,
        "downtime_seconds": downtime,
        # KI-021: "100% up, 3 reconnects" is the honest line; "100%" alone is
        # the flattering one. An RTMP reconnect is a real ~2.5s gap in what a
        # viewer saw, and it is invisible to every other number in this report.
        "reconnects": sum(
            1 for e in ordered if e["event_type"] == "stream_reconnected"
        ),
        "outages": [
            {
                "start": o["start"].isoformat(),
                "end": o["end"].isoformat(),
                "duration_seconds": (o["end"] - o["start"]).total_seconds(),
                "reason": o["reason"],
            }
            for o in outages
        ],
    }


def compute_broadcast_uptime(
    events: list[dict], window_start: datetime, window_end: datetime
) -> dict:
    """Fold broadcast_* events into **public-broadcast uptime** — how long
    YouTube was actually live, which is a different (and harsher) number than
    `compute_uptime`'s OBS-was-pushing uptime. On 2026-07-27 the encoder pushed
    for ~4h at 100% OBS uptime while nothing was public; reporting only the
    first number would have called that a successful soak.

    Note the *opposite default* from `compute_uptime`: downtime is proved by
    events, so no events means full OBS uptime; live time is also proved by
    events, so no events means 0% public uptime. Absence of evidence of being
    live is not evidence of being live.

    Events **before** `window_start` are honoured, not dropped. The sprint's
    design decision is one long-lived broadcast (`enableAutoStop=false`), so the
    `broadcast_live` that took the stream public routinely predates the report
    window by days and nothing at all lands inside it. Spans are clipped to the
    window; the caller is responsible for supplying the last live/ended event
    before `window_start` (see `main`).
    """
    ordered = sorted(events, key=lambda e: e["occurred_at"])
    spans: list[dict] = []
    live_since: datetime | None = None
    for event in ordered:
        occurred = datetime.fromisoformat(event["occurred_at"])
        etype = event["event_type"]
        if etype == "broadcast_live":
            if live_since is None:
                live_since = occurred
        elif etype == "broadcast_ended" and live_since is not None:
            spans.append({"start": live_since, "end": occurred})
            live_since = None
        # broadcast_created is bookkeeping — a bound broadcast isn't public yet.
    if live_since is not None:
        spans.append({"start": live_since, "end": window_end})

    clipped = []
    for span in spans:
        start = max(span["start"], window_start)
        end = min(span["end"], window_end)
        if end > start:
            clipped.append({"start": start, "end": end})

    live_seconds = sum((s["end"] - s["start"]).total_seconds() for s in clipped)
    window = (window_end - window_start).total_seconds()
    uptime_pct = round(100.0 * live_seconds / window, 2) if window else 0.0
    return {
        "uptime_pct": uptime_pct,
        "live_seconds": live_seconds,
        # 0% has two causes that must not be conflated: the broadcast really
        # wasn't public, or nothing is writing broadcast_* events at all
        # (manager not running, OAuth missing). Only the first is a stream
        # failure; reporting the second as one is a claim the data can't make.
        "measured": bool(ordered),
        "spans": [
            {
                "start": s["start"].isoformat(),
                "end": s["end"].isoformat(),
                "duration_seconds": (s["end"] - s["start"]).total_seconds(),
            }
            for s in clipped
        ],
    }


def compute_director_activity(events: list[dict], window_hours: float) -> dict:
    """Count director actions (scene_switched / commentary_spoken) into per-hour
    rates from the world_events log. Ignores non-director events. Suppressed
    lines are in-process only and never reach the log, so they aren't reported."""
    lines = [e for e in events if e["event_type"] == "commentary_spoken"]
    switches = [e for e in events if e["event_type"] == "scene_switched"]
    by_character: dict[str, int] = {}
    for event in lines:
        who = (event.get("payload") or {}).get("character", "unknown")
        by_character[who] = by_character.get(who, 0) + 1
    hours = window_hours or 1.0
    return {
        "lines": len(lines),
        "switches": len(switches),
        "lines_per_hour": round(len(lines) / hours, 2),
        "switches_per_hour": round(len(switches) / hours, 2),
        "by_character": by_character,
    }


def _report_event_types() -> list[str]:
    """The types the report actually folds — sourced from the registries that
    own them, so adding a stream/broadcast/director event can't leave the
    report silently blind to it."""
    from broadcast.events import BROADCAST_EVENT_TYPES
    from director.events import DIRECTOR_EVENT_TYPES
    from world.stream_events import STREAM_EVENT_TYPES

    return sorted(STREAM_EVENT_TYPES | BROADCAST_EVENT_TYPES | DIRECTOR_EVENT_TYPES)


def _fetch_report_events(fetch, window_start, per_type_limit: int = 5000) -> tuple:
    """Fetch the report's events **per type** (KI-014).

    One `since` query capped at N returns the *newest* N, so a busy window
    silently drops its oldest rows — the `stream_started` that opened it, or an
    early `broadcast_live`. Both understate uptime, on the report that is
    Sprint 11's acceptance evidence, with no sign anything was lost. Per-type
    queries keep each fold's own events well inside the cap, and hitting it is
    reported rather than swallowed.

    Returns `(events, truncated_types)`.
    """
    events: list[dict] = []
    truncated: list[str] = []
    for event_type in _report_event_types():
        rows = fetch(limit=per_type_limit, event_type=event_type, since=window_start)
        if len(rows) >= per_type_limit:
            truncated.append(event_type)
        events.extend(rows)
    return events, truncated


def _broadcast_events_for_window(
    fetch, window_events: list[dict], window_start
) -> list:
    """In-window broadcast events plus the last live/ended *before* the window.

    Without the prior events a long-lived broadcast (the design: one broadcast,
    `enableAutoStop=false`) leaves the window empty and the report would claim
    0% public uptime while the stream was public the entire time. Fetched per
    type — a broad `since` query is capped by `limit` and would silently drop
    the older rows, which are exactly the ones that matter here.
    """
    prior = []
    for event_type in ("broadcast_live", "broadcast_ended"):
        # The limit has to clear everything of this type *inside* the window,
        # or the prior event is pushed off the newest-first page and the
        # window's opening span is lost — KI-014's shape, one function over.
        # 500 is far past any plausible 24h recreate count.
        earlier = [
            e
            for e in fetch(limit=500, event_type=event_type)  # newest first
            if datetime.fromisoformat(e["occurred_at"]) < window_start
        ]
        if earlier:
            prior.append(earlier[0])
    inside = [e for e in window_events if e["event_type"].startswith("broadcast_")]
    return prior + inside


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream soak report")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    from storage.postgres_store import get_world_events

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=args.hours)
    all_events, truncated = _fetch_report_events(get_world_events, window_start)
    stream_events = [e for e in all_events if e["event_type"].startswith("stream_")]
    report = compute_uptime(stream_events, window_start, window_end)
    broadcast_events = _broadcast_events_for_window(
        get_world_events, all_events, window_start
    )
    broadcast = compute_broadcast_uptime(broadcast_events, window_start, window_end)
    activity = compute_director_activity(all_events, args.hours)
    print(f"# Soak report — {window_end.date()}\n")
    print(f"- Window: {window_start.isoformat()} → {window_end.isoformat()}")
    if truncated:
        # Say it loudly: an understated uptime read as a real failure is worse
        # than an admittedly incomplete one (KI-014).
        print(
            f"- ⚠️ **Incomplete data** — hit the per-type row cap for "
            f"{', '.join(truncated)}; the numbers below understate uptime."
        )
    print(
        f"- **Uptime: {report['uptime_pct']}%** "
        f"({report['downtime_seconds']:.0f}s down)"
    )
    # Printed unconditionally, including the 0 case. A number that only appears
    # when it is bad teaches the reader that its absence means "not measured".
    print(
        f"- Ingest reconnects: **{report['reconnects']}** "
        f"(OBS re-dialled RTMP on its own; invisible to the uptime figure above)"
    )
    if broadcast["measured"]:
        print(
            f"- **Public-broadcast uptime: {broadcast['uptime_pct']}%** "
            f"({broadcast['live_seconds']:.0f}s live on YouTube)"
        )
    else:
        print(
            "- Public-broadcast uptime: **not measured** — no broadcast_* events "
            "in the window (is broadcast_manager running?)"
        )
    print(f"- Outages: {len(report['outages'])}\n")
    if report["outages"]:
        print("| start | duration (s) | reason |")
        print("|---|---|---|")
        for o in report["outages"]:
            print(f"| {o['start']} | {o['duration_seconds']:.0f} | {o['reason']} |")
    # The two uptimes answer different questions: OBS uptime is "was the
    # encoder pushing", public uptime is "could anyone watch". A gap between
    # them is the 2026-07-27 failure mode, so it gets called out rather than
    # left for the reader to spot.
    # Gated on `measured`: with no broadcast events at all this would fire on
    # every report (including before the manager exists), and a warning that
    # cries wolf is ignored by the time it's real.
    gap = report["uptime_pct"] - broadcast["uptime_pct"]
    if broadcast["measured"] and gap > 1.0:
        print(
            f"\n> ⚠️ OBS was pushing {gap:.2f} percentage points longer than the "
            f"broadcast was public — encoder up, stream not watchable."
        )
    if broadcast["spans"]:
        print("\n| live from | duration (s) |")
        print("|---|---|")
        for s in broadcast["spans"]:
            print(f"| {s['start']} | {s['duration_seconds']:.0f} |")
    print("\n## Director activity\n")
    print(f"- Lines spoken: {activity['lines']} ({activity['lines_per_hour']}/h)")
    print(
        f"- Scene switches: {activity['switches']} ({activity['switches_per_hour']}/h)"
    )
    if activity["by_character"]:
        print("\n| character | lines |")
        print("|---|---|")
        for who, count in sorted(activity["by_character"].items()):
            print(f"| {who} | {count} |")


if __name__ == "__main__":
    main()
