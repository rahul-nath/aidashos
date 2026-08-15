#!/usr/bin/env bash
set -euo pipefail

export LOCAL_AGENT_DATABASE_URL="${LOCAL_AGENT_DATABASE_URL:-postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent}"
export LOCAL_AGENT_COORDINATION_BACKEND="${LOCAL_AGENT_COORDINATION_BACKEND:-postgres}"
export LOCAL_AGENT_COORDINATION_DATABASE_URL="${LOCAL_AGENT_COORDINATION_DATABASE_URL:-$LOCAL_AGENT_DATABASE_URL}"
export LOCAL_AGENT_MOCK_MODELS="${LOCAL_AGENT_MOCK_MODELS:-true}"
export LOCAL_AGENT_USE_DBOS="${LOCAL_AGENT_USE_DBOS:-false}"

uv run local-agent init-db
uv run local-agent harness
