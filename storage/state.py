import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable state file %s: %s", path, e)
        return {}
    if not isinstance(state, dict):
        logger.warning("Ignoring malformed state file %s: not a mapping", path)
        return {}
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a crash mid-write can't corrupt the state file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)
