"""The data/ guard (tests/data_guard.py) and the tmp redirect in tests/conftest.py.

Sprint 19 draft KI "tests write into data/raw": three API tests wrote fake CSV
snapshots into the checkout's real data/raw. These pin the two halves of the fix:
the redirect that keeps every test off data/, and the session guard that fails the
run if one ever reaches it again.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from data_guard import (
    LIVE_STATE_FILES,
    REPO_DATA_DIR,
    assert_tree_unchanged,
    snapshot_tree,
    tree_changes,
)

REPO = Path(__file__).resolve().parents[2]


def _changes(root, mutate):
    before = snapshot_tree(root)
    mutate()
    return tree_changes(before, snapshot_tree(root))


# --- the snapshot and the comparison ---------------------------------------


def test_a_missing_data_dir_snapshots_as_empty(tmp_path):
    assert snapshot_tree(tmp_path / "data") == {}


def test_the_snapshot_holds_names_sizes_and_mtimes(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.csv").write_text("abc")
    os.utime(tmp_path / "raw" / "a.csv", ns=(5, 7_000_000_000))

    assert snapshot_tree(tmp_path) == {
        ".": None,
        "raw/": None,
        "raw/a.csv": (3, 7_000_000_000),
    }


def test_nothing_changed_is_no_changes(tmp_path):
    (tmp_path / "a.csv").write_text("x")
    assert _changes(tmp_path, lambda: None) == []


def test_creating_the_data_dir_itself_is_a_change(tmp_path):
    root = tmp_path / "data"
    assert _changes(root, root.mkdir) == ["created:  ."]


def test_a_created_file_and_directory_are_changes(tmp_path):
    def write():
        (tmp_path / "raw").mkdir()
        (tmp_path / "raw" / "AAPL_1d.csv").write_text("x")

    assert _changes(tmp_path, write) == [
        "created:  raw/",
        "created:  raw/AAPL_1d.csv",
    ]


def test_a_rewritten_file_with_a_new_size_is_a_change(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    assert _changes(tmp_path, lambda: f.write_text("xy")) == ["modified: a.csv"]


def test_a_rewrite_that_only_moves_the_mtime_is_a_change(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    os.utime(f, ns=(1, 1_000_000_000))
    assert _changes(tmp_path, lambda: os.utime(f, ns=(1, 2_000_000_000))) == [
        "modified: a.csv"
    ]


def test_a_deleted_file_is_a_change(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    assert _changes(tmp_path, f.unlink) == ["deleted:  a.csv"]


def test_a_deleted_directory_is_a_change(tmp_path):
    (tmp_path / "raw").mkdir()
    assert _changes(tmp_path, lambda: shutil.rmtree(tmp_path / "raw")) == [
        "deleted:  raw/"
    ]


def test_live_service_state_files_are_not_compared_but_their_neighbours_are(tmp_path):
    for name in sorted(LIVE_STATE_FILES):
        (tmp_path / name).write_text("{}")

    def rewrite_all():
        for name in LIVE_STATE_FILES:
            (tmp_path / name).write_text('{"newer": true}')
        (tmp_path / "other.json").write_text("{}")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "ops_alert_state.json").write_text("{}")

    assert _changes(tmp_path, rewrite_all) == [
        "created:  other.json",
        "created:  sub/",
        "created:  sub/ops_alert_state.json",
    ]


def test_assert_tree_unchanged_fails_and_names_what_changed(tmp_path):
    before = snapshot_tree(tmp_path)
    (tmp_path / "AAPL_1d_2026.csv").write_text("x")

    with pytest.raises(pytest.fail.Exception) as raised:
        assert_tree_unchanged(tmp_path, before)

    assert "created:  AAPL_1d_2026.csv" in str(raised.value)


def test_assert_tree_unchanged_is_silent_when_nothing_changed(tmp_path):
    (tmp_path / "a.csv").write_text("x")
    assert_tree_unchanged(tmp_path, snapshot_tree(tmp_path))


# --- the redirect ----------------------------------------------------------


def test_the_guard_watches_the_data_root_production_resolves_to():
    """Load storage/naming.py fresh, untouched by the redirect: if production's
    root ever moves, the guard would otherwise keep watching an empty path."""
    spec = importlib.util.spec_from_file_location(
        "naming_unpatched", REPO / "storage" / "naming.py"
    )
    naming = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(naming)

    assert naming._DATA_ROOT.resolve() == REPO_DATA_DIR.resolve()


def test_every_raw_path_resolves_under_the_per_test_tmp_dir(tmp_path):
    from storage.naming import raw_data_path, raw_event_path, raw_news_path

    paths = [
        raw_data_path("AAPL", "1d"),
        raw_event_path("AAPL", "dividends"),
        raw_news_path("AAPL"),
    ]

    for path in paths:
        assert path.is_relative_to(tmp_path / "data" / "raw"), path
        assert not path.resolve().is_relative_to(REPO_DATA_DIR.resolve()), path


# --- the guard, end to end -------------------------------------------------

_BODIES = {
    # name: (seed files under data/, test body, expected exit code, expected text)
    "clean": ({}, "pass", 0, None),
    "create": ({}, 'DATA.mkdir(); (DATA / "leak.csv").write_text("x")', 1, "leak.csv"),
    # Collection runs before any fixture: only a snapshot taken at session start sees this.
    "import_time": (
        {},
        "pass",
        1,
        "import_leak.csv",
    ),
    "modify": (
        {"old.csv": "a"},
        '(DATA / "old.csv").write_text("bb")',
        1,
        "modified: old.csv",
    ),
    "live_state_only": (
        {"ops_alert_state.json": "{}"},
        '(DATA / "ops_alert_state.json").write_text("{1}")',
        0,
        None,
    ),
}


@pytest.mark.parametrize("scenario", sorted(_BODIES))
def test_a_session_that_writes_under_data_fails_the_run(tmp_path, scenario):
    """Run a real pytest session in a scratch project that carries copies of
    tests/conftest.py and tests/data_guard.py, so the real fixture wiring (the
    autouse, the session scope, the failing teardown) is what is under test."""
    seed, body, want_code, want_text = _BODIES[scenario]
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    for name in ("conftest.py", "data_guard.py"):
        shutil.copy(REPO / "tests" / name, project / "tests" / name)
    for name, text in seed.items():
        (project / "data").mkdir(exist_ok=True)
        (project / "data" / name).write_text(text)
    (project / "tests" / "test_body.py").write_text(
        "from pathlib import Path\n"
        'DATA = Path(__file__).resolve().parent.parent / "data"\n'
        + (
            'DATA.mkdir(exist_ok=True)\n(DATA / "import_leak.csv").write_text("x")\n'
            if scenario == "import_time"
            else ""
        )
        + f"\n\ndef test_body():\n    {body}\n"
    )

    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTEST_ADDOPTS": ""}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(project), str(project / "tests")],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )

    assert run.returncode == want_code, run.stdout + run.stderr
    if want_text:
        assert want_text in run.stdout, run.stdout
