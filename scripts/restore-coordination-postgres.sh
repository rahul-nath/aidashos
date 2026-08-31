#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DUMP_PATH="${1:-}"
TARGET_DATABASE="${2:-}"
POSTGRES_USER="${LOCAL_AGENT_POSTGRES_USER:-postgres}"

if [ ! -f "$DUMP_PATH" ] || [[ "$TARGET_DATABASE" != local_agent_restore_* ]]; then
  echo "Usage: restore-coordination-postgres.sh <database.dump> local_agent_restore_<name>" >&2
  echo "The target prefix prevents this script from overwriting a production database." >&2
  exit 2
fi
case "$TARGET_DATABASE" in
  *[!A-Za-z0-9_]*)
    echo "Restore target contains invalid characters: $TARGET_DATABASE" >&2
    exit 2
    ;;
esac

CHECKSUM_PATH="$DUMP_PATH.sha256"
if [ ! -f "$CHECKSUM_PATH" ]; then
  echo "Missing checksum: $CHECKSUM_PATH" >&2
  exit 2
fi
(cd "$(dirname "$DUMP_PATH")" && shasum -a 256 -c "$(basename "$CHECKSUM_PATH")")

docker compose exec -T postgres dropdb --username "$POSTGRES_USER" --if-exists "$TARGET_DATABASE"
docker compose exec -T postgres createdb --username "$POSTGRES_USER" "$TARGET_DATABASE"
docker compose exec -T postgres \
  pg_restore --username "$POSTGRES_USER" --dbname "$TARGET_DATABASE" \
    --exit-on-error --no-owner --no-privileges < "$DUMP_PATH"
echo "Restored $DUMP_PATH into disposable database $TARGET_DATABASE"
