"""Compatibility shim: the OBS scene spec moved to stream/scene.py (#43).

Not an entry point (it has no CLI); it only keeps `from scripts import
stream_scene` resolving to the one real module for any out-of-tree importer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stream.scene as _impl  # noqa: E402

sys.modules[__name__] = _impl
