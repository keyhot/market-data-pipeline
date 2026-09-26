"""The one seam that touches the network: plain SMTP + STARTTLS, Gmail by
default, credentials from the environment (a Gmail *app password*, not the
account password). Free, no new dependency.

`smtplib.SMTP` is looked up at call time, not bound at import, so the test
suite's refusal of it (tests/conftest.py) holds for this module too.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


class AlertsNotConfigured(RuntimeError):
    """ALERT_SMTP_USER / ALERT_SMTP_PASSWORD are not set."""


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str
    password: str
    to: str


def config_from_env() -> MailConfig:
    user = os.environ.get("ALERT_SMTP_USER", "").strip()
    # Google shows an app password as four space-separated groups; it never
    # contains whitespace itself, so a pasted-as-shown value still works.
    password = "".join(os.environ.get("ALERT_SMTP_PASSWORD", "").split())
    if not user or not password:
        raise AlertsNotConfigured(
            "set ALERT_SMTP_USER and ALERT_SMTP_PASSWORD (a Gmail app password) in .env"
        )
    return MailConfig(
        host=os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com"),
        port=int(os.environ.get("ALERT_SMTP_PORT", "587")),
        user=user,
        password=password,
        to=os.environ.get("ALERT_EMAIL_TO", "").strip() or user,
    )


def send_email(subject: str, body: str, config: MailConfig, smtp_factory=None) -> None:
    factory = smtp_factory or smtplib.SMTP
    msg = EmailMessage()
    msg["From"] = config.user
    msg["To"] = config.to
    msg["Subject"] = subject
    msg.set_content(body)
    with factory(config.host, config.port, timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(config.user, config.password)
        smtp.send_message(msg)
