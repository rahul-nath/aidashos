#!/usr/bin/env bash
# Postgres/DBOS smoke test against disposable databases.
#
# This script used to default every database to the operator's live ones, so a
# smoke run wrote sagas, dispatch intents, and execution leases straight into
# the coordination ledger an operator reads to decide what to work on. Sagas
# named "junior delegate smoke" and a dispatch intent sourced "codex_pg_smoke"
# came from exactly this path, and `gc_ledger` does not collect sagas, so they
# stayed until someone deleted them by hand.
#
# The databases below are created, used, and dropped. Override them only to
# point at another disposable pair, never at `local_agent`.
set -euo pipefail

SMOKE_DB="${LOCAL_AGENT_SMOKE_DATABASE:-local_agent_smoke}"
SMOKE_DBOS_DB="${LOCAL_AGENT_SMOKE_DBOS_DATABASE:-local_agent_smoke_dbos}"
PG_HOST="${LOCAL_AGENT_SMOKE_PG_HOST:-127.0.0.1:5432}"
PG_USER="${LOCAL_AGENT_SMOKE_PG_USER:-postgres}"
PG_PASSWORD="${LOCAL_AGENT_SMOKE_PG_PASSWORD:-postgres}"
PG_CONTAINER="${LOCAL_AGENT_SMOKE_PG_CONTAINER:-local-agent-postgres}"

# Refuse to run against the operator ledger even when the names are overridden.
# A smoke test that *can* write to `local_agent` is the bug this script had.
for db in "$SMOKE_DB" "$SMOKE_DBOS_DB"; do
    case "$db" in
        local_agent | local_agent_dbos)
            echo "refusing to smoke against the operator database '$db'" >&2
            exit 2
            ;;
    esac
done

base_url="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@${PG_HOST}"
export LOCAL_AGENT_DATABASE_URL="${base_url}/${SMOKE_DB}"
export LOCAL_AGENT_COORDINATION_BACKEND=postgres
export LOCAL_AGENT_COORDINATION_DATABASE_URL="$LOCAL_AGENT_DATABASE_URL"
export LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL="${base_url}/${SMOKE_DBOS_DB}"
export DBOS_SYSTEM_DATABASE_URL="$LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL"
export LOCAL_AGENT_MOCK_MODELS="${LOCAL_AGENT_MOCK_MODELS:-true}"
export LOCAL_AGENT_USE_DBOS="${LOCAL_AGENT_USE_DBOS:-true}"
export LOCAL_AGENT_SKIP_SUDO_FOR_MODEL_LOAD="${LOCAL_AGENT_SKIP_SUDO_FOR_MODEL_LOAD:-true}"

# Keep the coordination root off the operator's tree too: the sqlite adapter
# and the events.jsonl mirror both resolve from it, and `repo_root` otherwise
# walks up to the nearest .git and writes .agent_coordination/ into the repo.
# Two names because two mechanisms read it: coordination/store.py reads
# AGENT_COORDINATION_ROOT directly, Settings reads the LOCAL_AGENT_ one.
SMOKE_ROOT="$(mktemp -d -t local-agent-smoke)"
export AGENT_COORDINATION_ROOT="$SMOKE_ROOT"
export LOCAL_AGENT_COORDINATION_ROOT="$SMOKE_ROOT"

docker compose up -d postgres

drop_smoke_databases() {
    docker exec "$PG_CONTAINER" dropdb -U "$PG_USER" --if-exists --force "$SMOKE_DB" >/dev/null 2>&1 || true
    docker exec "$PG_CONTAINER" dropdb -U "$PG_USER" --if-exists --force "$SMOKE_DBOS_DB" >/dev/null 2>&1 || true
    rm -rf "$SMOKE_ROOT"
}
# Drop however this exits, so a failed run leaves nothing for the next one to
# inherit and read as real state.
trap drop_smoke_databases EXIT

drop_smoke_databases
docker exec "$PG_CONTAINER" createdb -U "$PG_USER" "$SMOKE_DB"
docker exec "$PG_CONTAINER" createdb -U "$PG_USER" "$SMOKE_DBOS_DB"

echo "smoking against ${SMOKE_DB} / ${SMOKE_DBOS_DB} (dropped on exit)"

LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 uv run pytest -q -m integration
uv run local-agent init-db
uv run pi /start
uv run pi "What owns durable workflow state?"
