"""#43: the paths the host actually executes must survive the move of the ops
logic out of scripts/.

systemd units, the cron line and compose run these files by path, from the repo
checkout, with the unit's interpreter. The logic now lives in stream/ and tools/;
what is pinned here is the *entry*: each path exists, runs its top level without
touching the network or starting its main loop, exposes a callable main(), and
keeps its CLI (`--help`, `-m`) and argparse prog name.

The units themselves live in ~/.config/systemd/user, not in the repo, so the
table below is the repo's record of what they run (verified 2026-10-22 against
`systemctl --user cat` / `crontab -l` / docker-compose.yml). A host-only test
cross-checks that table against the real unit files when they exist.

Never `--help` the watchdog, director or broadcast service here: the watchdog has
no argparse, so any argv would start its real OBS loop. They get the import probe.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# script path (the units' WorkingDirectory is the repo root) -> who runs it
LIVE_PY_ENTRYPOINTS = {
    "scripts/ops_alert.py": "ops-alert.service, fired by ops-alert.timer every 5 min",
    "scripts/music_bed.py": "stream-music.service",
    "scripts/stream_watchdog.py": "stream-watchdog.service",
    "director/service.py": "stream-director.service",
    "broadcast/service.py": "stream-broadcast.service",
    "scripts/liquidation_recorder.py": "docker-compose.yml (recorder)",
}
LIVE_SHELL_ENTRYPOINTS = {
    "scripts/backup_postgres.sh": "host cron 03:10 (BACKUP_TRIGGER=cron)",
}
# Documented in CLAUDE.md / the streaming-ops skill; run by hand, not by a unit.
HAND_RUN_PY_ENTRYPOINTS = (
    "scripts/stream_ctl.py",
    "scripts/soak_report.py",
    "scripts/measure_chair.py",
    "scripts/prepare_plate.py",
)
# Safe to run with --help: argparse is built and parsed before any I/O.
HELP_SAFE = (
    "scripts/ops_alert.py",
    "scripts/music_bed.py",
    "scripts/stream_ctl.py",
    "scripts/soak_report.py",
    "scripts/prepare_plate.py",
)
# scripts/ files that became thin wrappers over stream/ and tools/.
WRAPPERS = {
    "scripts/stream_watchdog.py": "stream.watchdog",
    "scripts/soak_report.py": "stream.soak_report",
    "scripts/measure_chair.py": "tools.measure_chair",
    "scripts/prepare_plate.py": "tools.prepare_plate",
    "scripts/stream_scene.py": "stream.scene",
}

_PROBE = """
import runpy, socket

def _no_network(*args, **kwargs):
    raise AssertionError("network access while importing an entry point")

socket.socket.connect = _no_network
socket.create_connection = _no_network
socket.getaddrinfo = _no_network
namespace = runpy.run_path({script!r}, run_name="_probe")
impl = namespace.get("_impl")
main = namespace.get("main") or getattr(impl, "main", None)
assert callable(main), "no callable main() behind " + {script!r}
"""


def _neutral_env():
    # Empty PYTHONPATH and a cwd that is not the repo: only the entry point's own
    # sys.path insert can make the repo importable, exactly as under systemd.
    return {**os.environ, "PYTHONPATH": ""}


@pytest.mark.parametrize(
    "script",
    [*LIVE_PY_ENTRYPOINTS, *HAND_RUN_PY_ENTRYPOINTS],
)
def test_entrypoint_exists_and_imports_without_side_effects(script, tmp_path):
    path = REPO / script
    assert path.is_file(), f"{script} is run by the host and no longer exists"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(script=str(path))],
        cwd=tmp_path,
        env=_neutral_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "an entry point must print nothing on import"


@pytest.mark.parametrize("script", list(LIVE_SHELL_ENTRYPOINTS))
def test_shell_entrypoint_exists_and_is_executable(script):
    path = REPO / script
    assert path.is_file(), f"{script} is run by cron and no longer exists"
    assert os.access(path, os.X_OK), f"{script} is invoked directly and must be +x"


@pytest.mark.parametrize("script", HELP_SAFE)
def test_help_runs_from_any_cwd_with_the_script_name_as_prog(script, tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO / script), "--help"],
        cwd=tmp_path,
        env=_neutral_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"usage: {Path(script).name}" in result.stdout


def test_module_form_still_works():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.soak_report", "--help"],
        cwd=REPO,
        env=_neutral_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "--hours" in result.stdout


@pytest.mark.parametrize("script", list(WRAPPERS))
def test_scripts_wrapper_stays_thin(script):
    source = (REPO / script).read_text()
    assert len(source.splitlines()) <= 25, f"{script} is growing logic again"
    assert not re.search(r"^\s*(def|class) ", source, re.M), (
        f"{script} must stay an entry point; its logic lives in {WRAPPERS[script]}"
    )
    assert f"import {WRAPPERS[script]} as _impl" in source


@pytest.mark.parametrize("script, module", sorted(WRAPPERS.items()))
def test_importing_the_old_dotted_path_yields_the_real_module(script, module):
    import importlib

    old = importlib.import_module("scripts." + Path(script).stem)
    assert old is importlib.import_module(module)


_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


@pytest.mark.skipif(not _UNIT_DIR.is_dir(), reason="no user systemd units on this host")
def test_every_python_file_a_host_unit_runs_is_in_the_table():
    seen = set()
    for unit in _UNIT_DIR.glob("*.service"):
        text = unit.read_text()
        if "market-data-pipeline" not in text:
            continue
        for match in re.finditer(r"^ExecStart=\S*python\S*\s+(\S+\.py)", text, re.M):
            seen.add(match.group(1))
    missing = seen - set(LIVE_PY_ENTRYPOINTS)
    assert not missing, f"a host unit runs {sorted(missing)}: add it to the table"
