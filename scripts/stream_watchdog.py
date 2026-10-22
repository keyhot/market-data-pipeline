"""Entry point: stream watchdog (systemd stream-watchdog.service).

The logic lives in stream/watchdog.py (#43). This file only keeps the
`python scripts/stream_watchdog.py` path, its argv and its exit status working.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stream.watchdog as _impl  # noqa: E402

if __name__ == "__main__":
    _impl.main()
else:  # `from scripts import stream_watchdog` keeps resolving to the real module
    sys.modules[__name__] = _impl
