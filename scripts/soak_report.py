"""Soak-test report (Sprint 11): summarize stream uptime from stream_* world
events as markdown. Failures found by the soak are the SUCCESS output of the
test — same truth-over-vanity rule as the backtest.
"""

import argparse
from datetime import datetime, timedelta, timezone


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
        if etype == "stream_dropped":
            degraded = payload.get("reason") == "dropped_frames"
            if degraded:
                # Degraded = live-but-impaired, zero downtime. Record and move
                # on — leaving it sitting in open_outage would block a real
                # outage that fires before the next stream_started, deleting
                # that outage's downtime from the report.
                outages.append(
                    {"start": occurred, "end": occurred, "reason": "dropped_frames"}
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream soak report")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    from storage.postgres_store import get_world_events

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=args.hours)
    all_events = get_world_events(limit=10_000, since=window_start)
    stream_events = [e for e in all_events if e["event_type"].startswith("stream_")]
    report = compute_uptime(stream_events, window_start, window_end)
    activity = compute_director_activity(all_events, args.hours)
    print(f"# Soak report — {window_end.date()}\n")
    print(f"- Window: {window_start.isoformat()} → {window_end.isoformat()}")
    print(
        f"- **Uptime: {report['uptime_pct']}%** "
        f"({report['downtime_seconds']:.0f}s down)"
    )
    print(f"- Outages: {len(report['outages'])}\n")
    if report["outages"]:
        print("| start | duration (s) | reason |")
        print("|---|---|---|")
        for o in report["outages"]:
            print(f"| {o['start']} | {o['duration_seconds']:.0f} | {o['reason']} |")
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
