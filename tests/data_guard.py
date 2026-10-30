"""Snapshot-and-compare guard for the checkout's data/ tree.

Why it exists (Sprint 19 draft KI "tests write into data/raw"): three API tests
mocked only the fetcher, so the real handler resolved `storage.naming._DATA_ROOT`
to `<checkout>/data`, wrote fake CSV snapshots there, and `save_csv` then pruned
to five files per prefix -- deleting real snapshots in a checkout that had them.
Nothing failed, because nothing watched. tests/conftest.py now redirects the data
root to a tmp dir for every test AND runs `assert_tree_unchanged` over the real
`data/` from session start to session end, so the next test to write there fails the run.

Pure functions, no pytest state, so tests/unit/test_data_guard.py can pin them.
"""

import os
from pathlib import Path

import pytest

# The checkout's real data directory, where `storage.naming._DATA_ROOT` points in
# production and where save_csv prunes real snapshots. Nothing in the suite may
# create, modify or delete anything under it.
REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Written by live services that run from the main checkout, not by tests: the
# ops alerter (ops-alert.timer, every 5 minutes) and the broadcast manager, each
# atomically through a `.tmp` sibling. Comparing them would fail a suite run in
# that checkout for no reason of the suite's. Top-level names only; the suite
# redirects both through OPS_ALERT_STATE / BROADCAST_STATUS_PATH (tests/conftest.py).
LIVE_STATE_FILES = frozenset(
    {
        "ops_alert_state.json",
        "ops_alert_state.tmp",
        "broadcast_status.json",
        "broadcast_status.tmp",
    }
)

# {relative posix path: (size, mtime_ns)} for a file, None for a directory (a
# directory's own mtime moves with every atomic replace inside it, so only its
# name is compared). "." stands for the root itself.
Snapshot = dict[str, tuple[int, int] | None]


def snapshot_tree(root: Path) -> Snapshot:
    """Every entry under `root`; {} when `root` does not exist."""
    root = Path(root)
    if not root.is_dir():
        return {}
    snap: Snapshot = {".": None}

    def walk(directory: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:  # removed between listing and descent
            return
        for entry in entries:
            rel = Path(entry.path).relative_to(root).as_posix()
            if rel in LIVE_STATE_FILES:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    snap[rel + "/"] = None
                    walk(entry.path)
                else:
                    st = entry.stat(follow_symlinks=False)
                    snap[rel] = (st.st_size, st.st_mtime_ns)
            except FileNotFoundError:  # removed between listing and stat
                continue

    walk(str(root))
    return snap


def tree_changes(before: Snapshot, after: Snapshot) -> list[str]:
    """One line per created, modified or deleted entry; [] when identical."""
    lines = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before:
            lines.append(f"created:  {name}")
        elif name not in after:
            lines.append(f"deleted:  {name}")
        elif before[name] != after[name]:
            lines.append(f"modified: {name}")
    return lines


def assert_tree_unchanged(root: Path, before: Snapshot) -> None:
    """Fail loudly if anything under `root` differs from `before`."""
    changes = tree_changes(before, snapshot_tree(root))
    if not changes:
        return
    shown = "\n".join(f"  {line}" for line in changes[:25])
    more = f"\n  ... and {len(changes) - 25} more" if len(changes) > 25 else ""
    pytest.fail(
        f"the test session changed the checkout's data tree ({root}):\n"
        f"{shown}{more}\n"
        "A test reached a path under the real data/ -- storage.naming._DATA_ROOT "
        "is redirected to a tmp dir for every test in tests/conftest.py, so this "
        "is a module-scoped fixture, a collection-time call, a subprocess, or a "
        "writer that does not use _DATA_ROOT. save_csv also prunes to five files "
        "per prefix, so a leak can delete real snapshots. Re-run the suspect "
        "tests alone (nothing else touches data/) to find the writer.",
        pytrace=False,
    )
