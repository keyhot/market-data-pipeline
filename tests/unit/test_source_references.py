"""Shipped comments may only point at things a clone can open (KI-077).

`.superpowers/` is gitignored and has zero tracked files, so a comment citing
`task-10-report.md` is a dead end at exactly the place a reader was told to
look for the evidence. The class re-entered after being fixed once
(`e2e2a3c`, KI-055 MINOR 5), which is why it is pinned here rather than
corrected a third time by hand: the evidence goes in the comment or in the
vault, never in a path only this worktree has.
"""

import re
import subprocess

from world.plate import DEFAULT_MANIFEST_PATH

REPO_ROOT = DEFAULT_MANIFEST_PATH.resolve().parents[2]

# Paths that do not survive a clone: the SDD working directory, and the task
# reports that live in it under any name.
UNREACHABLE = re.compile(r"\.superpowers/|task-\d+[-\w]*\.md|the Task \d+ report")

# Vendored renderer libraries: third-party minified source, not ours to police.
SKIP_PREFIXES = ("api/static/",)
SKIP_SUFFIXES = (".png", ".jpg", ".ico", ".lock", ".dump")


def _tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for name in out.split("\0"):
        if not name or name.startswith(SKIP_PREFIXES) or name.endswith(SKIP_SUFFIXES):
            continue
        if name == "tests/unit/test_source_references.py":  # states the patterns
            continue
        if name == ".gitignore":  # it is what makes the directory unreachable
            continue
        yield name


def test_no_tracked_file_points_the_reader_at_an_untracked_one():
    offenders = []
    for name in _tracked_text_files():
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if UNREACHABLE.search(line):
                offenders.append(f"{name}:{number}: {line.strip()[:100]}")
    assert not offenders, (
        "these tracked lines send the reader to a path no clone has "
        "(inline the conclusion, or put the evidence in the vault and link "
        "it there):\n" + "\n".join(offenders)
    )
