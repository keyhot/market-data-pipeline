"""Liquidations recorder entry point (C1). Opt-in compose profile `recorder`:

    docker compose --profile recorder up -d liquidations

Records Binance, OKX and Bybit forced liquidations into Postgres, with the
minutes it can prove it was listening (ingestion/liquidation_recorder.py).
Gaps are permanent — no venue serves free liquidation history — so this is
worth running continuously, and its value is elapsed time.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from ingestion.liquidation_recorder import (  # noqa: E402
    LiquidationRecorder,
    recorder_enabled,
)
from storage.liquidation_store import write_liquidation_batch  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> int:
    init_logging("INFO")
    if not recorder_enabled():
        logger.info("Liquidation recorder disabled (LIQUIDATION_RECORDER_ENABLED); exiting")
        return 0
    recorder = LiquidationRecorder(write_liquidation_batch)
    try:
        asyncio.run(recorder.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
