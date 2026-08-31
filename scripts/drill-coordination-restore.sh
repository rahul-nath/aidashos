#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${LOCAL_AGENT_BACKUP_DIR:-$HOME/.local-agent/backups/postgres}"
POSTGRES_USER="${LOCAL_AGENT_POSTGRES_USER:-postgres}"
LATEST="$(find "$BACKUP_DIR" -mindepth 2 -maxdepth 2 -name COMPLETE -print 2>/dev/null | sort | tail -1)"

if [ -z "$LATEST" ]; then
  echo "No complete backup exists under $BACKUP_DIR" >&2
  echo "Run: $ROOT/scripts/backup-coordination-postgres.sh" >&2
  exit 2
fi
BACKUP_SET="$(dirname "$LATEST")"
RESTORED=()

cleanup() {
  for database in "${RESTORED[@]}"; do
    docker compose exec -T postgres \
      dropdb --username "$POSTGRES_USER" --if-exists "$database" >/dev/null
  done
}
trap cleanup EXIT

for dump in "$BACKUP_SET"/*.dump; do
  source_name="$(basename "$dump" .dump)"
  target="local_agent_restore_${source_name}_$$"
  "$ROOT/scripts/restore-coordination-postgres.sh" "$dump" "$target"
  RESTORED+=("$target")
  table_count="$(docker compose exec -T postgres psql \
    --username "$POSTGRES_USER" --dbname "$target" --tuples-only --no-align \
    --command "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema');")"
  if [ "${table_count//[[:space:]]/}" -eq 0 ]; then
    echo "Restore drill found no public tables in $target" >&2
    exit 1
  fi
done

coordination_target="$(printf '%s\n' "${RESTORED[@]}" | awk '/local_agent_restore_local_agent_[0-9]+$/ {print; exit}')"
if [ -n "$coordination_target" ]; then
  LOCAL_AGENT_COORDINATION_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/$coordination_target" \
    AGENT_COORDINATION_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/$coordination_target" \
    uv run agent-ledger --no-next-commands list_work_units >/dev/null
fi
echo "Restore drill passed for $(basename "$BACKUP_SET")"
