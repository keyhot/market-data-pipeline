"""The Docker build context must not carry local dumps or the forecast venv.

`COPY . .` bakes the whole working tree into every image. `backups/` holds
Postgres dumps (about 2.5 GB) and `.venv-forecast/` the torch venv (about
1 GB); neither is read inside a container (`scripts/ops_alert.py` runs on the
host, the forecast venv is host-only), so both stay out of the build context.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _entries(name):
    lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def test_dockerignore_excludes_dumps_and_forecast_venv():
    entries = _entries(".dockerignore")
    assert "backups/" in entries
    assert ".venv-forecast/" in entries


def test_dockerignore_keeps_what_the_containers_read():
    # logs/ is opened by config/logging.py; model/ holds the code the scheduler
    # imports (model.predict). Neither may be excluded by this guard's own change.
    entries = _entries(".dockerignore")
    assert "logs/" not in entries
    assert "model/" not in entries


def test_gitignore_blocks_untracked_screenshots_under_data():
    assert "data/*.png" in _entries(".gitignore")
