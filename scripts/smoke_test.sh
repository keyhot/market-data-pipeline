#!/usr/bin/env bash
# End-to-end smoke test (Sprint 8): boot the full stack, wait for a stored
# crypto bar, and assert the API serves it. Exits 0 on success.
#
#   ./scripts/smoke_test.sh            # boot, verify, leave running
#   KEEP_DOWN=1 ./scripts/smoke_test.sh  # compose down when finished
set -euo pipefail
cd "$(dirname "$0")/.."

API="http://localhost:${API_PORT:-8000}"
SYMBOL="BTCUSDT"

say() { printf '[smoke] %s\n' "$*"; }
fail() { say "FAIL: $*"; docker compose logs --tail 30 api scheduler; exit 1; }

say "booting stack (docker compose up -d --build)…"
docker compose up -d --build

say "waiting for API /health…"
for i in $(seq 1 60); do
  if curl -fsS "$API/health" >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && fail "API /health never came up"
  sleep 2
done

say "checking Postgres connectivity via /health…"
curl -fsS "$API/health" | grep -q '"connected": *true' \
  || curl -fsS "$API/health" | grep -q '"connected":true' \
  || fail "/health reports Postgres not connected"

say "waiting for a stored $SYMBOL 1m bar (websocket backfill/stream)…"
for i in $(seq 1 90); do
  if curl -fsS "$API/bars/$SYMBOL?interval=1m&limit=1" >/dev/null 2>&1; then break; fi
  [ "$i" = 90 ] && fail "no 1m bars stored for $SYMBOL within 3 minutes"
  sleep 2
done
say "stored bars present."

say "asserting /chart/$SYMBOL?interval=1m renders…"
curl -fsS "$API/chart/$SYMBOL?interval=1m" | grep -q "const SYMBOL = \"$SYMBOL\"" \
  || fail "/chart page did not render"

say "asserting /stream/bars/$SYMBOL emits (SSE)…"
# `|| true` guards curl's SIGPIPE exit when head closes the pipe (pipefail).
SSE_HEAD=$(timeout 10 curl -sN "$API/stream/bars/$SYMBOL?interval=1m&poll_seconds=1" \
  | head -c 20 || true)
[ -n "$SSE_HEAD" ] || fail "SSE stream produced no output"

if [ "${KEEP_DOWN:-0}" = "1" ]; then
  say "compose down…"
  docker compose down
fi

say "PASS — stack boots, ingests, and serves live crypto bars."
