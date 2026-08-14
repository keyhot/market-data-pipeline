import logging
import logging.config
import re
import time
from pathlib import Path

_LOGS_DIR = Path(__file__).parent.parent / "logs"

sensitive_keys = (
    "headers",
    "credentials",
    "Authorization",
    "token",
    "password",
)


# mask sensitive data in record.msg
# KI-016: masking used to apply only to dict/list payloads, so a *formatted
# string* carrying a secret walked straight through — obsws_python logs
# `password='...'` in its connect line, and it landed verbatim in the journal
# every 30 seconds. Third-party libraries will keep doing this, so the filter
# now redacts the shape rather than trusting every dependency.
_SECRET_HINTS = ("password", "passwd", "token", "secret", "key")
_SECRET_IN_TEXT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key)\b(\s*[=:]\s*)"
    r"('[^']*'|\"[^\"]*\"|\S+)"
)


# Everything the logging machinery puts on a record itself. Anything else was
# passed by a caller through `extra=`, and is therefore something someone
# wanted in the log. Derived from a real record rather than hard-coded,
# because the set grows between Python versions (`taskName` in 3.12).
_RESERVED_RECORD_KEYS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None))
) | {"message", "asctime", "taskName"}


def context_fields(record) -> dict:
    """The `extra=` fields on a record, in the order they were given."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_KEYS and not key.startswith("_")
    }


class ContextFormatter(logging.Formatter):
    """Render `extra=` fields, which the bare format string drops on the floor.

    This is KI-020's root cause, and it was never local to the director:
    eleven call sites across the scheduler, the ingester, the broadcast
    manager, the storage writer and the trader mirror all reported failures as
    ``logger.warning("... failed", extra={"error": str(e)})``, and the format
    string is ``%(message)s``. Every one of them logged that something broke
    and threw away what. Twenty-five hours of identical, causeless warnings is
    what that costs; fixing it at each call site would have left the next one
    to rediscover it.
    """

    def format(self, record):
        base = super().format(record)
        context = context_fields(record)
        if not context:
            return base
        rendered = " ".join(f"{key}={value!r}" for key, value in context.items())
        return f"{base} | {rendered}"


class SensitiveDataFilter(logging.Filter):
    sensitive_keys = sensitive_keys

    def filter(self, record):
        # `extra=` values reach the formatter without ever passing through
        # `record.msg`, so rendering them (above) would have walked a secret
        # straight into the journal — the exact KI-016 shape, reintroduced by
        # its own fix. Masked here, where the sanitiser already lives.
        for key, value in context_fields(record).items():
            if key in self.sensitive_keys:
                setattr(record, key, "******")
            elif isinstance(value, (dict, list, tuple)):
                setattr(record, key, sanitize(value))
            elif isinstance(value, str):
                setattr(record, key, _SECRET_IN_TEXT.sub(r"\1\2******", value))

        if isinstance(record.msg, (dict, list, tuple)):
            record.msg = sanitize(record.msg)
            return True
        # Cheap gate first. `record.getMessage()` formats msg % args eagerly,
        # and this filter runs on every record the logger admits — doing that
        # unconditionally under DEBUG made the test suite crawl. The *key* is
        # always in the format string even when the value arrives via args, so
        # scanning it is both sufficient and nearly free.
        if not isinstance(record.msg, str):
            return True
        lowered = record.msg.lower()
        if not any(hint in lowered for hint in _SECRET_HINTS):
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True  # never let masking break logging itself
        redacted = _SECRET_IN_TEXT.sub(r"\1\2******", message)
        if redacted != message:
            # Only rewrite records that actually carried a secret; args are
            # folded in because the value may have arrived through them.
            record.msg = redacted
            record.args = ()
        return True


def sanitize(obj):
    if isinstance(obj, dict):
        return {
            k: ("******" if k in sensitive_keys else sanitize(v))
            for k, v in obj.items()
        }
    elif isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    elif hasattr(obj, "__dict__"):
        return sanitize(vars(obj))
    else:
        return obj


def init_logging(log_level: str = "DEBUG") -> logging.Logger:
    LOGGING_CONFIG = {
        "version": 1,
        # Must stay False: app module loggers (scheduler.jobs, ingestion.*)
        # are created at import time, before init_logging() runs — True
        # silently disables them all (found during Sprint 8 smoke testing).
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": ContextFormatter,
                "format": "%(asctime)s | [%(levelname)s] | %(name)s | %(message)s",
            },
        },
        "filters": {
            "sensitive_data_filter": {
                "()": SensitiveDataFilter,
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": log_level,
                "stream": "ext://sys.stdout",
                "filters": ["sensitive_data_filter"],
            },
            "file": {
                "formatter": "default",
                "class": "logging.handlers.RotatingFileHandler",
                "level": log_level,
                "filename": str(_LOGS_DIR / "logs.log"),
                "mode": "a",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "filters": ["sensitive_data_filter"],
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": log_level,
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["console", "file"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["console", "file"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["console", "file"],
                "level": log_level,
                "propagate": False,
            },
            # KI-016: obsws_python logs its connection parameters — including
            # the websocket password — at INFO, and dumps full request/response
            # payloads at DEBUG. Neither tells us anything the watchdog and
            # director don't already log themselves, and both reached the
            # journal in cleartext every 30s. The redaction filter is the
            # safety net; not emitting it is the fix.
            "obsws_python": {
                "level": "WARNING",
                "propagate": True,
            },
        },
    }

    _LOGS_DIR.mkdir(exist_ok=True)
    logging.Formatter.converter = time.gmtime
    logging.config.dictConfig(LOGGING_CONFIG)
    logger = logging.getLogger(__name__)

    return logger
