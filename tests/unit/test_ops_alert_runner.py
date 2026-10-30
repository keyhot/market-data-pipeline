"""The alert runner's seams: delivery failure retries, missing credentials
degrade to a log line, and the suite can never send real mail (the KI-083
class — a populated .env on the streaming box would otherwise email the user
on every test run)."""

import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from alerts import mailer
from scripts import ops_alert

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _observe_failing():
    from alerts.rules import Check

    return [Check("world", True, "world stale")]


def test_the_suite_refuses_real_smtp():
    with pytest.raises(RuntimeError, match="no mail"):
        smtplib.SMTP("smtp.gmail.com", 587)
    with pytest.raises(RuntimeError, match="no mail"):
        smtplib.SMTP_SSL("smtp.gmail.com", 465)


def test_credentials_in_the_environment_still_send_nothing(monkeypatch, caplog):
    # The real sender is the default; the guard is what stops it here.
    monkeypatch.setenv("ALERT_SMTP_USER", "someone@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "app-password")
    caplog.set_level(logging.ERROR, logger="scripts.ops_alert")
    for i in range(3):
        ops_alert.run_once(
            observe=_observe_failing, now=NOW + timedelta(minutes=5 * i)
        )
    # Reaching here without a RuntimeError escaping means the failure was
    # contained; the state proves the alert was NOT recorded as delivered.
    state = json.loads(ops_alert.state_path().read_text())
    assert state["checks"]["world"]["firing"] is False
    assert state.get("undelivered_since")
    # And it was the suite's mail guard that stopped the send: the real sender
    # read the credentials, reached smtplib.SMTP and was refused. `firing`
    # alone cannot tell that from a config error, a crash before SMTP or a real
    # SMTP that merely fails offline, all of which also leave it False.
    failures = [
        r.getMessage() for r in caplog.records if "alert NOT delivered" in r.getMessage()
    ]
    assert failures
    assert all("RuntimeError: the test suite sends no mail" in m for m in failures)


def test_a_failed_delivery_is_retried_next_tick():
    sent = []

    def flaky(subject, body):
        if not sent:
            sent.append(None)
            raise OSError("smtp down")
        sent.append(subject)

    for i in range(4):
        ops_alert.run_once(
            observe=_observe_failing, send=flaky, now=NOW + timedelta(minutes=5 * i)
        )
    assert len([s for s in sent if s]) == 1
    assert "FIRING" in sent[-1]


def test_missing_credentials_raise_a_named_error(monkeypatch):
    monkeypatch.delenv("ALERT_SMTP_USER", raising=False)
    monkeypatch.delenv("ALERT_SMTP_PASSWORD", raising=False)
    with pytest.raises(mailer.AlertsNotConfigured):
        mailer.config_from_env()


def test_the_recipient_defaults_to_the_sender(monkeypatch):
    monkeypatch.setenv("ALERT_SMTP_USER", "me@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "x")
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    assert mailer.config_from_env().to == "me@example.com"


def test_the_mailer_speaks_starttls_then_login_then_send(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            calls.append(("quit",))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))

    cfg = mailer.MailConfig("smtp.gmail.com", 587, "me@example.com", "pw", "me@example.com")
    mailer.send_email("subj", "body", cfg, smtp_factory=FakeSMTP)
    assert [c[0] for c in calls] == ["connect", "starttls", "login", "send", "quit"]
    assert calls[0][1:] == ("smtp.gmail.com", 587)


def test_the_backup_status_is_read_from_the_file_the_script_writes(tmp_path):
    status = tmp_path / "last_status.json"
    status.write_text(json.dumps({"stamp": "2026-09-26T01-10-01Z", "ok": True}))
    assert ops_alert.read_json(status)["ok"] is True
    assert ops_alert.read_json(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert ops_alert.read_json(tmp_path / "bad.json") is None


def test_a_long_delivery_outage_is_not_mistaken_for_a_gap():
    # The runner WAS watching; only the mail failed. A gap notice on top of the
    # retried alert would claim the host slept when it did not.
    def down(subject, body):
        raise OSError("smtp down")

    for i in range(12):  # an hour of failed deliveries
        ops_alert.run_once(
            observe=_observe_failing, send=down, now=NOW + timedelta(minutes=5 * i)
        )
    sent = []
    alerts = ops_alert.run_once(
        observe=_observe_failing,
        send=lambda s, b: sent.append(s),
        now=NOW + timedelta(minutes=60),
    )
    assert [a.kind for a in alerts] == ["firing"]


def test_a_crashing_check_is_reported_not_fatal_to_the_others(monkeypatch, tmp_path):
    # A malformed status file must not silence every check on every run —
    # that is the no-path-to-a-human failure this alerter exists to end.
    bad = tmp_path / "broadcast_status.json"
    bad.write_text('{"updated_at": "2026-09-26T12:00:00+00:00", "refused_transitions": "lots"}')
    monkeypatch.setenv("BROADCAST_STATUS_PATH", str(bad))
    monkeypatch.setattr(ops_alert, "_fetch_health", lambda: (None, "down"))
    checks = {c.name: c for c in ops_alert.observe_system(NOW)}
    assert set(checks) == {"backup", "world", "broadcast"}
    assert checks["broadcast"].failing is True
    assert "crashed" in checks["broadcast"].summary


def test_a_gmail_app_password_pasted_with_its_spaces_still_works(monkeypatch):
    # Google displays app passwords as four space-separated groups.
    monkeypatch.setenv("ALERT_SMTP_USER", "me@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "abcd efgh ijkl mnop")
    assert mailer.config_from_env().password == "abcdefghijklmnop"
