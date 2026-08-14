"""KI-016: a third-party library logged the OBS websocket password in cleartext
to the journal on every connect. The masking filter only ever sanitized dict
payloads, so a formatted string walked straight through it."""

import io
import logging

from config.logging import (
    ContextFormatter,
    SensitiveDataFilter,
    init_logging,
)


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


def _emit(message, **extra):
    """Format one record the way a handler would, filter included."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ContextFormatter("%(levelname)s | %(message)s"))
    handler.addFilter(SensitiveDataFilter())
    logger = logging.getLogger("test_context_formatter")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.warning(message, extra=extra)
    return stream.getvalue().strip()


def test_extra_fields_are_rendered_not_dropped():
    """KI-020's root cause, and it was never local to the director. Eleven call
    sites reported failures as `extra={"error": str(e)}` against a format
    string of `%(message)s`, so every one of them logged *that* something
    broke and discarded *what*. Twenty-five hours of causeless warnings is
    what that cost."""
    out = _emit("Trader mirror skipped", error="ConnectionRefusedError(111)")
    assert "Trader mirror skipped" in out
    assert "ConnectionRefusedError(111)" in out, out


def test_a_record_without_extras_is_unchanged():
    """No trailing separator on the overwhelming majority of lines."""
    assert _emit("Scene rebuilt after OBS recovery") == (
        "WARNING | Scene rebuilt after OBS recovery"
    )


def test_a_secret_passed_through_extra_is_still_masked():
    """Rendering `extra` would otherwise walk a secret into the journal by a
    route the filter never inspected — KI-016's shape, reintroduced by the fix
    for KI-020. Both the sensitive *key* and a secret inside a value."""
    out = _emit("connecting", password="hunter2")
    assert "hunter2" not in out and "******" in out, out

    nested = _emit("client built", detail="token=abc123 host=127.0.0.1")
    assert "abc123" not in nested, nested
    assert "127.0.0.1" in nested, "only the secret goes"

    payload = _emit("request", body={"password": "hunter2", "symbol": "BTC"})
    assert "hunter2" not in payload and "BTC" in payload, payload


def test_no_call_site_still_routes_an_exception_into_a_dropped_field():
    """The formatter renders `extra` now, so these are no longer silent — this
    pins the *reason* they are safe. If the format string is ever narrowed
    back, this is the test that should have to be deleted deliberately."""
    from config.logging import ContextFormatter as _CF

    assert issubclass(_CF, logging.Formatter)
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), None)
    record.error = "ValueError('nope')"
    assert "ValueError('nope')" in _CF("%(message)s").format(record)


def test_obsws_library_is_quieted_below_warning():
    """Belt and braces: the library's INFO connect line and DEBUG payload dumps
    carry nothing we don't already log ourselves, and both leaked the password.
    Redaction is the safety net; not emitting it at all is the fix."""
    init_logging()
    assert logging.getLogger("obsws_python").getEffectiveLevel() >= logging.WARNING
