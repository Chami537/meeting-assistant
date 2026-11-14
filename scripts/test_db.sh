#!/usr/bin/env bash
# Throwaway PostgreSQL 16 for integration tests.
#   eval "$(scripts/test_db.sh up)"   # starts it and exports TEST_DATABASE_URL
#   scripts/test_db.sh down
set -euo pipefail
NAME=ma-test-pg
PORT=${PORT:-5434}
case "${1:-up}" in
  up)
    docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=test -p "127.0.0.1:$PORT:5432" postgres:16 >/dev/null
    until docker exec "$NAME" psql -U postgres -qAtc 'select 1' >/dev/null 2>&1; do sleep 1; done
    echo "export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:$PORT/postgres"
    ;;
  down)
    docker stop "$NAME" >/dev/null
    ;;
  *)
    echo "usage: $0 up|down" >&2
    exit 2
    ;;
esac
