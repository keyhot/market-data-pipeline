"""Entry point: stream soak report.

The logic lives in stream/soak_report.py (#43). This file only keeps the
`python scripts/soak_report.py` path, its argv and its exit status working.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stream.soak_report as _impl  # noqa: E402

if __name__ == "__main__":
    _impl.main()
else:  # `from scripts import soak_report` keeps resolving to the real module
    sys.modules[__name__] = _impl
