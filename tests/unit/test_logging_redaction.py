"""KI-016: a third-party library logged the OBS websocket password in cleartext
to the journal on every connect. The masking filter only ever sanitized dict
payloads, so a formatted string walked straight through it."""

import logging

from config.logging import SensitiveDataFilter, init_logging


def _redact(message, *args):
    record = logging.LogRecord(
        "obsws_python.baseclient.ObsClient", logging.INFO, __file__, 1,
        message, args, None,
    )
    SensitiveDataFilter().filter(record)
    return record.getMessage()


def test_password_in_a_formatted_string_is_redacted():
    """The exact shape observed in journalctl."""
    out = _redact(
        "Connecting with parameters: host='127.0.0.1' port=4455 "
        "password='mdp-stream-REDACTED-IN-TEST' subs=0 timeout=5.0"
    )
    assert "mdp-stream-REDACTED-IN-TEST" not in out
    assert "******" in out
    assert "host='127.0.0.1'" in out      # only the secret goes


def test_other_secret_keys_are_redacted_too():
    for key in ("token", "secret", "api_key", "apikey", "passwd"):
        out = _redact(f"connecting {key}='hunter2' port=1")
        assert "hunter2" not in out, key


def test_ordinary_messages_are_untouched():
    assert _redact("Scene rebuilt after OBS recovery") == (
        "Scene rebuilt after OBS recovery"
    )
    assert _redact("bars stored: %d", 42) == "bars stored: 42"


def test_obsws_library_is_quieted_below_warning():
    """Belt and braces: the library's INFO connect line and DEBUG payload dumps
    carry nothing we don't already log ourselves, and both leaked the password.
    Redaction is the safety net; not emitting it at all is the fix."""
    init_logging()
    assert logging.getLogger("obsws_python").getEffectiveLevel() >= logging.WARNING
