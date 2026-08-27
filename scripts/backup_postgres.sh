#!/usr/bin/env bash
# Nightly Postgres backup (Sprint 9). The world's memory (world_events) is
# irreplaceable — bars can be re-fetched, history cannot. Roadmap standing
# rule: nightly pg_dump from Sprint 9, offsite copy before Sprint 12.
#
#   ./scripts/backup_postgres.sh          # dump + verify + prune
#   Cron (host):  10 3 * * *  cd <repo> && BACKUP_TRIGGER=cron ./scripts/backup_postgres.sh >> backups/backup.log 2>&1
#
# KI-049: this script used to redirect straight into its final filename, so the
# shell created and truncated the destination BEFORE pg_dump ran — four nights
# of `service "postgres" is not running` left four 0-byte files with correct,
# reassuring names, and `set -e` aborted before the log line, so the only trace
# was an absence. Two rules now hold, and both are pinned by
# tests/unit/test_backup_postgres.py:
#
#   1. the dump is written to a `.part` file and only ever RENAMED into place
#      after it verifies — a failure leaves no file at all, which is a state
#      nobody mistakes for a backup;
#   2. retention keeps the newest N *verified* dumps rather than deleting by
#      age, so a run of failing nights can no longer prune away the good ones.
#
# Restore drill (run at least once per quarter — an untested backup is a hope):
#   docker compose exec -T postgres createdb -U market_data restore_drill
#   docker compose exec -T postgres pg_restore -U market_data -d restore_drill < backups/<latest>.dump
#   docker compose exec -T postgres psql -U market_data -d restore_drill -c "SELECT count(*) FROM price_bars; SELECT count(*) FROM world_events;"
#   docker compose exec -T postgres dropdb -U market_data restore_drill
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="backups"
KEEP=${BACKUP_KEEP:-14}          # newest N *verified* dumps
MIN_BYTES=${BACKUP_MIN_BYTES:-1024}
STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT="$BACKUP_DIR/market_data_$STAMP.dump"
PART="$OUT.part"
STATUS="$BACKUP_DIR/last_status.json"
# Who started this run. The 03:10 cron line should set BACKUP_TRIGGER=cron;
# a hand-run leaves "unknown", which is the honest answer and never claims
# a nightly happened. Anything reading last_status.json for freshness needs
# to be able to tell those apart.
TRIGGER=${BACKUP_TRIGGER:-unknown}

mkdir -p "$BACKUP_DIR"

# Write the outcome where something other than a human reading a log can find
# it. Failure used to be invisible: `set -e` aborted before the "[backup] ..."
# line, so twelve days of no backups looked exactly like twelve days of nothing
# happening.
write_status() {
  local ok="$1" bytes="$2" error="$3"
  # `|| true`, and it is the whole point of the line: this is the only step in
  # the failure path with a dependency of its own (python3, which cron's
  # environment does not guarantee — `sg docker -c` sources no profile, the
  # KI-017 class). If writing the status file took the FAILURE REPORT down with
  # it, a failed night would again be traceable only by absence, which is the
  # bug this script was fixed for.
  python3 - "$STATUS" "$ok" "$bytes" "$OUT" "$STAMP" "$TRIGGER" "$error" <<'PY' || true
import json, sys
path, ok, size, out, stamp, trigger, error = sys.argv[1:8]
json.dump(
    {
        "stamp": stamp,
        "ok": ok == "true",
        "bytes": int(size),
        "path": out,
        "trigger": trigger,
        "error": error or None,
    },
    open(path, "w"),
    indent=2,
)
PY
}

fail() {
  local message="$1"
  rm -f "$PART"
  # The cheap, dependency-free trace first: whatever else fails after this
  # line, the log says the backup did not happen.
  echo "[backup] FAILED: $message" >&2
  write_status false 0 "$message"
  exit 1
}

trap 'rm -f "$PART"' EXIT

# deployment_identity is excluded on purpose: it says whether a database is
# dev or prod (storage/db.py's guard), which is a property of the HOST, not of
# the data. Carrying it in the dump would restore a laptop's 'dev' stamp onto
# production. Each host gets its row from db/init.sql and is stamped once.
if ! docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-market_data}" \
     --exclude-table=deployment_identity \
     -Fc "${POSTGRES_DB:-market_data}" > "$PART"; then
  fail "pg_dump exited non-zero (see stderr above)"
fi

BYTES=$(wc -c < "$PART" | tr -d ' ')
if [ "$BYTES" -lt "$MIN_BYTES" ]; then
  fail "dump is $BYTES bytes, below the $MIN_BYTES-byte floor"
fi

# Size is not integrity. pg_restore lives in the container, not on this host,
# so the archive is parsed where the tool is: -l lists the TOC and exits
# non-zero on anything it cannot read.
if ! docker compose exec -T postgres pg_restore -l < "$PART" > /dev/null 2>&1; then
  fail "pg_restore could not read the archive ($BYTES bytes)"
fi

mv "$PART" "$OUT"
write_status true "$BYTES" ""
echo "[backup] $OUT ($(du -h "$OUT" | cut -f1), verified)"

# Retention is by validity, not by mtime — see scripts/backup_prune.py.
python3 scripts/backup_prune.py "$BACKUP_DIR" --keep "$KEEP"
