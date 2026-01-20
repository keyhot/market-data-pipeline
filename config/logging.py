import re
import time
import logging
import logging.config

sensitive_keys = (
    "headers",
    "credentials",
    "Authorization",
    "token",
    "password",
)


# mask sensitive data in record.msg
class SensitiveDataFilter(logging.Filter):
    sensitive_keys = sensitive_keys

    def filter(self, record):
        if isinstance(record.msg, (dict, list, tuple)):
            record.msg = sanitize(record.msg)
        return True

def sanitize(obj):
    if isinstance(obj, dict):
        return {k: ("******" if k in sensitive_keys else sanitize(v)) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    elif hasattr(obj, "__dict__"):
        return sanitize(vars(obj))
    else:
        return obj

def init_logging(log_level: str = "DEBUG", formatter: str = "console") -> logging.Logger:
    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "default": {
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
                "filename": "logs/logs.log",
                "mode": "a",
                "maxBytes": 10485760, #10MB
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
        },
    }

    logging.Formatter.converter = time.gmtime
    logging.config.dictConfig(LOGGING_CONFIG)
    logger = logging.getLogger(__name__)

    return logger