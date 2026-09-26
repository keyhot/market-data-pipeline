"""The alert runner's seams: delivery failure retries, missing credentials
degrade to a log line, and the suite can never send real mail (the KI-083
class — a populated .env on the streaming box would otherwise email the user
on every test run)."""

import json
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


def test_credentials_in_the_environment_still_send_nothing(monkeypatch):
    # The real sender is the default; the guard is what stops it here.
    monkeypatch.setenv("ALERT_SMTP_USER", "someone@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "app-password")
    for i in range(3):
        ops_alert.run_once(
            observe=_observe_failing, now=NOW + timedelta(minutes=5 * i)
        )
    # Reaching here without a RuntimeError escaping means the failure was
    # contained; the state proves the alert was NOT recorded as delivered.
    state = json.loads(ops_alert.state_path().read_text())
    assert state["checks"]["world"]["firing"] is False


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
