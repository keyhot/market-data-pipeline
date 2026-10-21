"""Entry point: prepare the world plate (art tool).

The logic lives in tools/prepare_plate.py (#43). This file only keeps the
`python scripts/prepare_plate.py` path, its argv and its exit status working.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.prepare_plate as _impl  # noqa: E402

if __name__ == "__main__":
    _impl.cli()
else:  # `from scripts import prepare_plate` keeps resolving to the real module
    sys.modules[__name__] = _impl
