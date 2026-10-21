"""Process-wide singletons and the symbol check every router shares (#44).

These lived at the top of `api/main.py`. They are here, in a module that
imports nothing from `api`, so a router can use them without importing
`api.main` (which imports the routers). Python caches the module, so each
object is still created exactly once per process.
"""

import re

from api.metrics import MetricsRegistry
from config.exceptions import BaseAppException
from config.logging import init_logging
from scheduler.service import SchedulerService

scheduler_service = SchedulerService()

logger = init_logging()

metrics_registry = MetricsRegistry()


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,15}$")


def _validated_symbol(raw: str) -> str:
    symbol = raw.upper()
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise BaseAppException(f"Invalid symbol: {raw!r}", status_code=400)
    return symbol
