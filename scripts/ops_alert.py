"""Ops alerter: one run per systemd-timer tick (every 5 minutes).

Reads the three failure records that had no path to a human — the backup's
`backups/last_status.json` (KI-049), `/health`'s `data.world` (KI-057) and the
broadcast manager's `data/broadcast_status.json` (KI-086) — and emails when
one starts failing, daily while it stays failing, and once when it recovers.

Runs on the host, not in the api container: `backups/` is not mounted there,
and the alerter must still speak when the API is the thing that is down.

    python scripts/ops_alert.py               # one run
    python scripts/ops_alert.py --dry-run     # print what would be sent; keep no state
    python scripts/ops_alert.py --test-email  # prove delivery end to end
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alerts import mailer  # noqa: E402
from alerts.rules import check_backup, check_broadcast, check_world, decide, render  # noqa: E402

logger = logging.getLogger(__name__)

# Above /health's own DB budget (KI-069, measured ~4s under load).
HEALTH_TIMEOUT_S = 15


def state_path() -> Path:
    return Path(os.environ.get("OPS_ALERT_STATE", ROOT / "data" / "ops_alert_state.json"))


def broadcast_status_path() -> Path:
    return Path(os.environ.get("BROADCAST_STATUS_PATH", ROOT / "data" / "broadcast_status.json"))


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _fetch_health() -> tuple[dict | None, str | None]:
    url = os.environ.get("OPS_ALERT_HEALTH_URL", "http://127.0.0.1:8000/health")
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as resp:
            return json.loads(resp.read()), None
    except Exception as exc:  # unreachable, timeout, non-JSON — all "could not look"
        return None, f"{type(exc).__name__}: {exc}"


def observe_system(now: datetime):
    health, error = _fetch_health()
    return [
        check_backup(read_json(ROOT / "backups" / "last_status.json"), now),
        check_world(health, error),
        check_broadcast(read_json(broadcast_status_path()), now),
    ]


def _send_real(subject: str, body: str) -> None:
    mailer.send_email(subject, body, mailer.config_from_env())


def _write_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def run_once(observe=None, send=None, now: datetime | None = None, dry_run=False) -> list:
    now = now or datetime.now(timezone.utc)
    send = send or _send_real
    state = read_json(state_path()) or {}
    checks = observe() if observe else observe_system(now)
    alerts, new_state = decide(checks, state, now)
    if dry_run:
        for check in checks:
            print(f"{'FAIL' if check.failing else 'ok  '} {check.name}: {check.summary}")
        if alerts:
            print("would send:\n  " + "\n  ".join(render(alerts)))
        return alerts
    if alerts:
        subject, body = render(alerts)
        try:
            send(subject, body)
            logger.info("alert sent: %s", subject)
        except Exception as exc:
            # Keep the OLD state: decide() never mutated it, so the same
            # alerts are computed and retried on the next run.
            logger.error(
                "alert NOT delivered, will retry next run: %s (%s)",
                subject,
                f"{type(exc).__name__}: {exc}",
            )
            kept = {**state, "undelivered_since": state.get("undelivered_since") or now.isoformat()}
            # The runner was watching even though the mail failed, so this run
            # counts against the gap clock — unless the undelivered alert IS
            # the gap notice, which must survive to be retried.
            if not any(a.kind == "gap" for a in alerts):
                kept["last_run_at"] = now.isoformat()
            _write_state(kept)
            return alerts
    new_state.pop("undelivered_since", None)
    _write_state(new_state)
    return alerts


def main(argv=None) -> int:
    from config.logging import init_logging

    init_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    args = parser.parse_args(argv)
    if args.test_email:
        _send_real(
            "[market-world] test alert",
            "If you can read this, the ops alerter can reach you.\n",
        )
        print("test email sent")
        return 0
    run_once(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
