#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${LOCAL_AGENT_BACKUP_DIR:-$HOME/.local-agent/backups/postgres}"
COPY_TARGET_FILE="${LOCAL_AGENT_BACKUP_COPY_TARGET_FILE:-$HOME/.local-agent/backup-copy-target}"
POSTGRES_USER="${LOCAL_AGENT_POSTGRES_USER:-postgres}"
DATABASES="${LOCAL_AGENT_BACKUP_DATABASES:-local_agent local_agent_dbos}"
ALLOW_SAME_DEVICE="${LOCAL_AGENT_BACKUP_ALLOW_SAME_DEVICE:-false}"

if [ ! -f "$COPY_TARGET_FILE" ]; then
  echo "Backup copy target is not configured." >&2
  echo "Write one absolute mounted/off-machine directory path to $COPY_TARGET_FILE" >&2
  exit 2
fi
IFS= read -r COPY_DIR < "$COPY_TARGET_FILE"
if [[ "$COPY_DIR" != /* || "$COPY_DIR" = "$BACKUP_DIR" ]]; then
  echo "Backup copy target must be an absolute directory distinct from $BACKUP_DIR" >&2
  exit 2
fi
if [ ! -d "$COPY_DIR" ]; then
  echo "Backup copy target is not mounted or missing: $COPY_DIR" >&2
  echo "The backup job will not create a directory that could masquerade as an off-machine mount." >&2
  exit 2
fi
mkdir -p "$BACKUP_DIR"
LOCAL_DEVICE="$(df -P "$BACKUP_DIR" | awk 'NR == 2 {print $1}')"
COPY_DEVICE="$(df -P "$COPY_DIR" | awk 'NR == 2 {print $1}')"
if [ "$ALLOW_SAME_DEVICE" != "true" ] && [ "$LOCAL_DEVICE" = "$COPY_DEVICE" ]; then
  echo "Backup copy target is on the same filesystem as the local backup." >&2
  echo "Use an external/network mount, or explicitly set LOCAL_AGENT_BACKUP_ALLOW_SAME_DEVICE=true for a synchronized directory." >&2
  exit 2
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_PARTIAL="$BACKUP_DIR/.${STAMP}.partial"
LOCAL_FINAL="$BACKUP_DIR/$STAMP"
COPY_PARTIAL="$COPY_DIR/.${STAMP}.partial"
COPY_FINAL="$COPY_DIR/$STAMP"
mkdir -p "$LOCAL_PARTIAL" "$COPY_PARTIAL"

cleanup_partial() {
  if [ -d "$LOCAL_PARTIAL" ]; then
    mv "$LOCAL_PARTIAL" "$BACKUP_DIR/${STAMP}.failed"
  fi
  if [ -d "$COPY_PARTIAL" ]; then
    mv "$COPY_PARTIAL" "$COPY_DIR/${STAMP}.failed"
  fi
}
trap cleanup_partial EXIT

for database in $DATABASES; do
  case "$database" in
    *[!A-Za-z0-9_]*)
      echo "Invalid database name in LOCAL_AGENT_BACKUP_DATABASES: $database" >&2
      exit 2
      ;;
  esac
  dump="$LOCAL_PARTIAL/$database.dump"
  docker compose exec -T postgres \
    pg_dump --username "$POSTGRES_USER" --dbname "$database" \
      --format custom --no-owner --no-privileges > "$dump"
  docker compose exec -T postgres pg_restore --list < "$dump" >/dev/null
  (cd "$LOCAL_PARTIAL" && shasum -a 256 "$database.dump" > "$database.dump.sha256")
done

printf 'created_at=%s\ndatabases=%s\n' "$STAMP" "$DATABASES" > "$LOCAL_PARTIAL/MANIFEST"
touch "$LOCAL_PARTIAL/COMPLETE"
cp -p "$LOCAL_PARTIAL"/* "$COPY_PARTIAL/"
mv "$LOCAL_PARTIAL" "$LOCAL_FINAL"
mv "$COPY_PARTIAL" "$COPY_FINAL"
trap - EXIT
echo "Backup complete: $LOCAL_FINAL"
echo "Off-machine copy complete: $COPY_FINAL"
