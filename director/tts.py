"""Local Piper TTS (Sprint 13): text -> wav via the ``piper`` binary, one voice
per personality. Piper is local, offline, and free — no per-line cost.

The subprocess runner is injected so tests never invoke the binary. **Degrades
to silence**: any failure (Piper missing, non-zero exit, no output) returns
None and the caller keeps the stream running — TTS must never take the stream
down. Actual playback into OBS is a media-source wiring step (go-live / docs).
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def synthesize(text, voice, out_path, runner=subprocess.run) -> Path | None:
    """Render ``text`` in ``voice`` to ``out_path``; return the path, or None on
    any failure (never raises)."""
    out_path = Path(out_path)
    piper_bin = os.environ.get("PIPER_BIN", "piper")
    voice_dir = os.environ.get("PIPER_VOICE_DIR", "voices")
    model = f"{voice_dir.rstrip('/')}/{voice}.onnx"
    cmd = [piper_bin, "-m", model, "-f", str(out_path)]
    try:
        result = runner(cmd, input=text, capture_output=True, text=True)
    except Exception as exc:  # binary missing / OS error — keep the stream alive
        logger.warning(
            "Piper TTS failed to run", extra={"error": str(exc), "voice": voice}
        )
        return None
    if getattr(result, "returncode", 1) != 0:
        logger.warning("Piper TTS returned nonzero", extra={"voice": voice})
        return None
    if not out_path.exists():
        logger.warning("Piper TTS produced no output", extra={"voice": voice})
        return None
    return out_path
