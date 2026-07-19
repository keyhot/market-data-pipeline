#!/usr/bin/env bash
# Nightly Postgres backup (Sprint 9). The world's memory (world_events) is
# irreplaceable — bars can be re-fetched, history cannot. Roadmap standing
# rule: nightly pg_dump from Sprint 9, offsite copy before Sprint 12.
#
#   ./scripts/backup_postgres.sh          # dump + prune (>14 days)
#   Cron (host):  10 3 * * *  cd <repo> && ./scripts/backup_postgres.sh >> backups/backup.log 2>&1
#
# Restore drill (run at least once per quarter — an untested backup is a hope):
#   docker compose exec -T postgres createdb -U market_data restore_drill
#   docker compose exec -T postgres pg_restore -U market_data -d restore_drill /dev/stdin < backups/<latest>.dump
#   docker compose exec -T postgres psql -U market_data -d restore_drill -c "SELECT count(*) FROM price_bars; SELECT count(*) FROM world_events;"
#   docker compose exec -T postgres dropdb -U market_data restore_drill
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="backups"
KEEP_DAYS=14
STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT="$BACKUP_DIR/market_data_$STAMP.dump"

mkdir -p "$BACKUP_DIR"

docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-market_data}" \
  -Fc "${POSTGRES_DB:-market_data}" > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "[backup] $OUT ($SIZE)"

find "$BACKUP_DIR" -name "market_data_*.dump" -mtime +"$KEEP_DAYS" -delete
echo "[backup] pruned dumps older than $KEEP_DAYS days"
