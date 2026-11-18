"""What counts as failing, and when an email goes out. Pure: no clock, no
network, no files — the runner supplies all three.

Each check is judged on its own; `decide` turns a run of checks into
firing / reminder / resolved alerts, debounced so a routine `up -d --build`
never emails anyone, plus one after-the-fact `gap` notice when the runner
itself was not running (KI-088: the host sleeps with its lid shut, and every
alerter on it sleeps too).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# backup_postgres.sh runs at 01:10Z nightly; one missed night is > 24h, and the
# extra hours absorb a slow dump without calling a late night a lost one.
BACKUP_MAX_AGE = timedelta(hours=27)
# The broadcast manager ticks every 30s; ten minutes of silence is a dead,
# hung or fail-closed (missing OAuth) manager.
BROADCAST_STALE_AFTER = timedelta(minutes=10)
# Twenty failed ticks is ten minutes of YouTube refusing to be read.
BROADCAST_TICK_FAILURES_AT = 20
# Consecutive failing runs before a check fires: at a 5-minute timer, ~10 min.
CONFIRM_TICKS = 3
REMIND_AFTER = timedelta(hours=24)
# The timer fires every 5 minutes; half an hour of nobody looking is a gap.
GAP_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class Check:
    name: str
    failing: bool
    summary: str


@dataclass(frozen=True)
class Alert:
    kind: str  # "firing" | "reminder" | "resolved" | "gap"
    check: str
    summary: str


def _parse_backup_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)


def check_backup(status: dict | None, now: datetime) -> Check:
    """KI-049: `backups/last_status.json` as backup_postgres.sh writes it."""
    if status is None:
        return Check(
            "backup", True, "no backup status file — has the nightly backup ever run?"
        )
    if not status.get("ok"):
        return Check("backup", True, f"last backup FAILED: {status.get('error')}")
    try:
        taken = _parse_backup_stamp(str(status.get("stamp")))
    except ValueError:
        return Check(
            "backup",
            True,
            f"backup status has an unreadable stamp {status.get('stamp')!r}",
        )
    age = now - taken
    if age > BACKUP_MAX_AGE:
        return Check(
            "backup",
            True,
            f"newest verified backup is {age.total_seconds() / 3600:.1f}h old",
        )
    return Check("backup", False, f"backup ok ({status.get('stamp')})")


def _hours(seconds) -> str:
    return "unknown" if seconds is None else f"{seconds / 3600:.1f}h"


def check_world(health: dict | None, error: str | None = None) -> Check:
    """KI-057: `/health`'s `data.world`. A world nobody could read is failing
    too — "we could not look" is not "we looked and it was fine"."""
    if health is None:
        return Check("world", True, f"API /health unreachable: {error}")
    data = health.get("data") or {}
    world = data.get("world")
    if world is None:
        reason = data.get("world_unavailable_reason")
        return Check("world", True, f"world liveness could not be read ({reason})")
    if world.get("stale"):
        return Check(
            "world",
            True,
            "the world has gone quiet: newest signal "
            f"{_hours(world.get('signal_age_s'))} old, newest event "
            f"{_hours(world.get('event_age_s'))} old",
        )
    return Check("world", False, "world is live")


def check_broadcast(status: dict | None, now: datetime) -> Check:
    """KI-086: the status the broadcast manager writes every tick."""
    from broadcast.service import REFUSED_TRANSITION_ALERT_AT

    if status is None:
        return Check(
            "broadcast",
            True,
            "broadcast manager has written no status — is it running?",
        )
    try:
        updated = datetime.fromisoformat(str(status.get("updated_at")))
    except ValueError:
        return Check("broadcast", True, "broadcast status has an unreadable timestamp")
    if now - updated > BROADCAST_STALE_AFTER:
        minutes = (now - updated).total_seconds() / 60
        return Check(
            "broadcast", True, f"broadcast manager last ticked {minutes:.0f} min ago"
        )
    refused = int(status.get("refused_transitions") or 0)
    if refused >= REFUSED_TRANSITION_ALERT_AT:
        return Check(
            "broadcast",
            True,
            f"YouTube refused {refused} consecutive go-live transitions of "
            f"{status.get('broadcast_id')} — is another broadcast live on our key?",
        )
    failures = int(status.get("consecutive_tick_failures") or 0)
    if failures >= BROADCAST_TICK_FAILURES_AT:
        return Check(
            "broadcast", True, f"broadcast manager's last {failures} ticks failed"
        )
    return Check("broadcast", False, f"broadcast {status.get('lifecycle')}")


def decide(
    checks: list[Check],
    state: dict,
    now: datetime,
    *,
    confirm_ticks: int = CONFIRM_TICKS,
    remind_after: timedelta = REMIND_AFTER,
    gap_after: timedelta = GAP_AFTER,
) -> tuple[list[Alert], dict]:
    """Return the alerts to send and the state to keep if they are delivered.
    Never mutates `state`: on a failed delivery the runner keeps the old one,
    which is exactly what makes the same alert go out again next run."""
    new = copy.deepcopy(state)
    alerts: list[Alert] = []

    last_run = new.get("last_run_at")
    if last_run is not None:
        gap = now - datetime.fromisoformat(last_run)
        if gap > gap_after:
            alerts.append(
                Alert(
                    "gap",
                    "alerter",
                    f"nothing was watching for {gap.total_seconds() / 3600:.1f}h "
                    f"(since {last_run}) — was the host asleep?",
                )
            )
    new["last_run_at"] = now.isoformat()

    per_check = new.setdefault("checks", {})
    for check in checks:
        s = per_check.setdefault(
            check.name, {"consecutive": 0, "firing": False, "last_sent_at": None}
        )
        if not check.failing:
            if s["firing"]:
                alerts.append(Alert("resolved", check.name, check.summary))
            s.update(consecutive=0, firing=False, last_sent_at=None)
            continue
        s["consecutive"] += 1
        if not s["firing"]:
            if s["consecutive"] >= confirm_ticks:
                alerts.append(Alert("firing", check.name, check.summary))
                s.update(firing=True, last_sent_at=now.isoformat())
        elif now - datetime.fromisoformat(s["last_sent_at"]) >= remind_after:
            alerts.append(Alert("reminder", check.name, check.summary))
            s["last_sent_at"] = now.isoformat()
    return alerts, new


def render(alerts: list[Alert]) -> tuple[str, str]:
    """One email per run, however many checks changed."""
    kinds = {
        "firing": "FIRING",
        "reminder": "STILL FAILING",
        "resolved": "RESOLVED",
        "gap": "GAP",
    }
    head = ", ".join(f"{kinds[a.kind]} {a.check}" for a in alerts)
    subject = f"[market-world] {head}"
    body = "\n".join(f"{kinds[a.kind]} — {a.check}: {a.summary}" for a in alerts)
    return subject, body + "\n"
