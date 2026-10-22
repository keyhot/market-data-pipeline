"""Entry point: measure the painted chair (art tool).

The logic lives in tools/measure_chair.py (#43). This file only keeps the
`python scripts/measure_chair.py` path, its argv and its exit status working.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.measure_chair as _impl  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_impl.main())
else:  # `from scripts import measure_chair` keeps resolving to the real module
    sys.modules[__name__] = _impl
