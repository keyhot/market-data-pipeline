"""KI-049 — the nightly backup wrote a zero-byte dump under a reassuring name.

Two halves, tested at the level each one lives at:

* the **shell** owns the ordering bug — `> "$OUT"` truncates the destination
  before `pg_dump` has said a word, so any failure leaves an empty file with a
  correct name. Those tests run the real script against a fake `docker` and
  assert on what is left in the directory afterwards.
* the **retention rule** owns the amplification — pruning by mtime deletes good
  dumps while failing nights keep writing empty ones. That rule is pure Python
  and unit-tested here.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.backup_prune import dumps_to_prune, is_verified_dump

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "backup_postgres.sh"

# The first bytes pg_dump -Fc writes. A file that does not start with these is
# not a custom-format archive, whatever its name says.
PGDMP = b"PGDMP\x01\x0f\x00\x04\x08\x01\x01\x00\x01\x00\x00"


# --------------------------------------------------------------------------
# the shell: what is left in backups/ when the dump does not happen
# --------------------------------------------------------------------------


def _fake_docker(bin_dir: Path, *, dump_exit=0, dump_bytes=b"", restore_exit=0):
    """A `docker` that answers the two calls the script makes.

    `pg_dump` writes `dump_bytes` to stdout and exits `dump_exit`;
    `pg_restore` exits `restore_exit`. Anything else is a hard failure, so a
    script that starts calling something new cannot pass by accident.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload = bin_dir / "payload.bin"
    payload.write_bytes(dump_bytes)
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *pg_dump*)\n"
        f'    cat "{payload}"\n'
        f"    exit {dump_exit} ;;\n"
        "  *pg_restore*)\n"
        "    cat > /dev/null\n"
        '    echo ";     1; 1259 16384 TABLE public price_bars market_data"\n'
        f"    exit {restore_exit} ;;\n"
        "esac\n"
        'echo "fake docker: unexpected call: $*" >&2\n'
        "exit 99\n"
    )
    fake.chmod(0o755)
    return fake


def _run_backup(tmp_path: Path, **fake) -> subprocess.CompletedProcess:
    """Run the real script in a throwaway copy of the repo layout."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, repo / "scripts" / SCRIPT.name)
    shutil.copy(REPO / "scripts" / "backup_prune.py", repo / "scripts")
    (repo / "docker-compose.yml").write_text("services: {}\n")

    bin_dir = tmp_path / "bin"
    _fake_docker(bin_dir, **fake)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
        timeout=60,
    )


def _dumps(tmp_path: Path):
    return sorted((tmp_path / "repo" / "backups").glob("market_data_*"))


def test_a_dump_that_succeeds_lands_under_its_final_name(tmp_path):
    result = _run_backup(tmp_path, dump_bytes=PGDMP + b"\x00" * 4096)

    assert result.returncode == 0, result.stderr
    names = [p.name for p in _dumps(tmp_path)]
    assert len(names) == 1, names
    assert names[0].startswith("market_data_") and names[0].endswith(".dump")


def test_a_failed_pg_dump_leaves_no_file_at_all(tmp_path):
    """The KI itself: `service "postgres" is not running` produced a 0-byte
    dump with a correct name, four nights running."""
    result = _run_backup(tmp_path, dump_exit=1)

    assert result.returncode != 0
    assert _dumps(tmp_path) == []


def test_an_empty_dump_is_never_kept(tmp_path):
    """pg_dump can exit 0 and still have written nothing worth keeping."""
    result = _run_backup(tmp_path, dump_bytes=b"")

    assert result.returncode != 0
    assert _dumps(tmp_path) == []


def test_an_unreadable_archive_is_never_kept(tmp_path):
    """Non-zero size is not integrity — the archive has to parse."""
    result = _run_backup(tmp_path, dump_bytes=b"not an archive", restore_exit=1)

    assert result.returncode != 0
    assert _dumps(tmp_path) == []


def test_failure_is_recorded_where_something_can_read_it(tmp_path):
    """`set -e` aborted before the log line, so the only trace of four failed
    nights was an absence. Failure now writes a status file."""
    _run_backup(tmp_path, dump_exit=1)

    status_file = tmp_path / "repo" / "backups" / "last_status.json"
    status = json.loads(status_file.read_text())
    assert status["ok"] is False
    assert status["stamp"]
    assert status["error"]


def test_success_is_recorded_too(tmp_path):
    """A status file that is only written on failure cannot answer 'is the
    backup healthy right now', which is the question that went unasked for 12
    days."""
    _run_backup(tmp_path, dump_bytes=PGDMP + b"\x00" * 4096)

    status_file = tmp_path / "repo" / "backups" / "last_status.json"
    status = json.loads(status_file.read_text())
    assert status["ok"] is True
    assert status["bytes"] > 0
    assert status["path"].endswith(".dump")


def test_a_failed_run_leaves_the_previous_good_dump_alone(tmp_path):
    """The dangerous version of this bug: a failing night that also prunes."""
    _run_backup(tmp_path, dump_bytes=PGDMP + b"\x00" * 4096)
    good = _dumps(tmp_path)
    assert len(good) == 1

    _run_backup(tmp_path, dump_exit=1)

    assert [p.name for p in _dumps(tmp_path)] == [good[0].name]


# --------------------------------------------------------------------------
# the retention rule: keep the newest N *verified* dumps
# --------------------------------------------------------------------------


def _dump_file(directory: Path, name: str, body: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(body)
    return path


def test_a_zero_byte_file_is_not_a_verified_dump(tmp_path):
    assert not is_verified_dump(_dump_file(tmp_path, "market_data_a.dump", b""))


def test_a_file_without_the_archive_magic_is_not_a_verified_dump(tmp_path):
    body = b"service \"postgres\" is not running\n" * 100
    assert not is_verified_dump(_dump_file(tmp_path, "market_data_b.dump", body))


def test_a_custom_format_archive_is_a_verified_dump(tmp_path):
    body = PGDMP + b"\x00" * 4096
    assert is_verified_dump(_dump_file(tmp_path, "market_data_c.dump", body))


def test_only_the_newest_verified_dumps_are_kept(tmp_path):
    files = [
        _dump_file(
            tmp_path, f"market_data_2026-08-{day:02d}.dump", PGDMP + b"\x00" * 99
        )
        for day in (10, 11, 12, 13)
    ]
    for offset, path in enumerate(files):
        os.utime(path, (1_700_000_000 + offset, 1_700_000_000 + offset))

    pruned = dumps_to_prune(tmp_path, keep=2)

    assert sorted(p.name for p in pruned) == [
        "market_data_2026-08-10.dump",
        "market_data_2026-08-11.dump",
    ]


def test_an_unverified_dump_never_counts_toward_the_keep_quota(tmp_path):
    """The 15-failed-nights scenario: empty dumps must not push good ones out
    of the window that protects them."""
    good = _dump_file(tmp_path, "market_data_old.dump", PGDMP + b"\x00" * 99)
    os.utime(good, (1_700_000_000, 1_700_000_000))
    for day in range(3):
        empty = _dump_file(tmp_path, f"market_data_empty_{day}.dump", b"")
        os.utime(empty, (1_700_001_000 + day, 1_700_001_000 + day))

    pruned = dumps_to_prune(tmp_path, keep=2)

    assert good not in pruned
    assert len(pruned) == 3


def test_fewer_verified_dumps_than_the_quota_prunes_none_of_them(tmp_path):
    for day in (10, 11):
        _dump_file(tmp_path, f"market_data_{day}.dump", PGDMP + b"\x00" * 99)

    assert dumps_to_prune(tmp_path, keep=14) == []


def test_pruning_ignores_files_that_are_not_dumps(tmp_path):
    (tmp_path / "backup.log").write_text("[backup] ...\n")
    (tmp_path / "last_status.json").write_text("{}")

    assert dumps_to_prune(tmp_path, keep=1) == []


def test_a_partial_dump_in_flight_is_never_pruned(tmp_path):
    """The script writes to `.dump.part` and renames on success; a prune that
    ran concurrently must not delete the run in progress."""
    part = _dump_file(tmp_path, "market_data_now.dump.part", b"")
    good = _dump_file(tmp_path, "market_data_old.dump", PGDMP + b"\x00" * 99)

    pruned = dumps_to_prune(tmp_path, keep=1)

    assert part not in pruned
    assert good not in pruned


@pytest.mark.parametrize("keep", [0, -1])
def test_a_nonsensical_keep_prunes_nothing(tmp_path, keep):
    """A misconfigured KEEP must fail safe: never 'keep zero backups'."""
    _dump_file(tmp_path, "market_data_a.dump", PGDMP + b"\x00" * 99)

    assert dumps_to_prune(tmp_path, keep=keep) == []
