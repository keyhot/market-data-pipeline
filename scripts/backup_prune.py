"""Retention for the nightly Postgres dumps — by validity, not by age (KI-049).

The old rule was `find ... -mtime +14 -delete`, which prunes on every
*successful* run regardless of what the surviving files contain. Fifteen
consecutive failed nights would therefore have deleted every good dump while
the job kept writing empty ones and reporting nothing; the August window
reached five of the fifteen.

The rule here is "keep the newest N **verified** dumps". A file that is not a
readable custom-format archive is not a backup, so it never occupies a slot in
that window — it is only ever a candidate for deletion.

Pure and stdlib-only on purpose: `scripts/backup_postgres.sh` runs from host
cron, outside the poetry environment.
"""

import argparse
import sys
from pathlib import Path

# The first five bytes pg_dump -Fc writes. Cheap, and it is the difference
# between "a file with the right name" and "an archive".
ARCHIVE_MAGIC = b"PGDMP"

DUMP_GLOB = "market_data_*.dump"
DEFAULT_KEEP = 14


def is_verified_dump(path: Path) -> bool:
    """Is this file a custom-format archive with something in it?

    Deliberately not a full `pg_restore -l` parse: that needs the postgres
    client tools, which this host does not have outside the container. The
    script checks the archive with `pg_restore -l` *in* the container at write
    time; this is the standing check that keeps a file in the keep window.
    """
    try:
        if path.stat().st_size <= len(ARCHIVE_MAGIC):
            return False
        with path.open("rb") as handle:
            return handle.read(len(ARCHIVE_MAGIC)) == ARCHIVE_MAGIC
    except OSError:
        return False


def dumps_to_prune(directory, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Every dump file that is not one of the newest `keep` verified dumps.

    Unverified files (empty, truncated, an error message under a dump's name)
    are always pruned — they are the KI's evidence, and keeping them is what
    made a failure look like a backup. Files still being written carry a
    `.part` suffix and so never match the glob.
    """
    directory = Path(directory)
    if keep < 1:
        # A misconfigured KEEP must never mean "keep zero backups".
        return []

    dumps = sorted(
        (p for p in directory.glob(DUMP_GLOB) if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )

    survivors: list[Path] = []
    doomed: list[Path] = []
    for path in dumps:
        if is_verified_dump(path) and len(survivors) < keep:
            survivors.append(path)
        else:
            doomed.append(path)
    return doomed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="the backups directory")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be pruned and delete nothing",
    )
    args = parser.parse_args(argv)

    doomed = dumps_to_prune(args.directory, keep=args.keep)
    for path in doomed:
        why = "empty or unreadable" if not is_verified_dump(path) else "beyond keep"
        if args.dry_run:
            print(f"[backup] would prune {path} ({why})")
            continue
        try:
            path.unlink()
            print(f"[backup] pruned {path} ({why})")
        except OSError as exc:  # pragma: no cover - reported, never fatal
            print(f"[backup] could not prune {path}: {exc}", file=sys.stderr)

    remaining = [p for p in Path(args.directory).glob(DUMP_GLOB) if is_verified_dump(p)]
    print(
        f"[backup] pruned {len(doomed)}, "
        f"{len(remaining)} verified dumps kept (limit {args.keep})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
