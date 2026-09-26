"""Alerts that reach a human (KI-049 / KI-057 / KI-086).

Five durable failure records — the JSONL spools, `backups/last_status.json`,
`world_events`, `/health`, the soak report — had no path to a person, and
every outage the project has had was found by somebody looking. `rules` is the
pure half (what is failing, when to say so); `mailer` is the one seam that
touches the network; `scripts/ops_alert.py` is the systemd-timer runner.
"""
