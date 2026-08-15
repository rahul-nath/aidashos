-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- Provision the read-only privilege that agents hold on the DBOS system database.
--
-- The system database records every workflow this application has executed, and
-- for durable_workflow_entrypoint the `inputs` column holds ingress event
-- payloads. This role can read the metadata an agent needs to answer "has this
-- ever run?" and is refused by Postgres on everything else: the grant is at the
-- COLUMN level, so `SELECT inputs FROM dbos.workflow_status` fails with
-- "permission denied for table workflow_status" rather than returning content.
--
-- That is the point. src/local_first_agent_os/durable_execution_ledger.py also
-- keeps its projection closed, but that is a property of today's code. This is a
-- property of the connection, and it survives whatever the code is edited into.
--
-- Run as a superuser against the DBOS system database:
--
--   docker exec -i local-agent-postgres \
--     psql -U postgres -d local_agent_dbos -v reader_password="'<password>'" \
--     -f - < scripts/grant_execution_ledger_reader.sql
--
-- Then hand the resulting URL to the agent process, and only to it:
--
--   LOCAL_AGENT_LEDGER_READER_DATABASE_URL=postgresql://agent_ledger_reader:<password>@127.0.0.1:5432/local_agent_dbos
--
-- Re-running is safe. The role is created once and the grants are re-applied,
-- which is also how you repair a grant someone dropped.

\set ON_ERROR_STOP on

-- A password is required rather than defaulted, because a default password on a
-- role that reads production metadata is worse than no role at all.
\if :{?reader_password}
\else
\echo 'ERROR: pass the password as -v reader_password="''...''"'
\quit 1
\endif

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ledger_reader') THEN
        CREATE ROLE agent_ledger_reader LOGIN;
    END IF;
END
$$;

ALTER ROLE agent_ledger_reader WITH PASSWORD :reader_password;

-- Start from nothing, so a re-run narrows a widened grant instead of adding to it.
REVOKE ALL ON ALL TABLES IN SCHEMA dbos FROM agent_ledger_reader;
REVOKE ALL ON SCHEMA dbos FROM agent_ledger_reader;

GRANT CONNECT ON DATABASE local_agent_dbos TO agent_ledger_reader;
GRANT USAGE ON SCHEMA dbos TO agent_ledger_reader;

-- The whole privilege. `name` and `status` answer which workflows exist and how
-- they ended; count(*) reads no column at all, so the tallies work without one.
GRANT SELECT (name, status) ON dbos.workflow_status TO agent_ledger_reader;
GRANT SELECT (function_name) ON dbos.operation_outputs TO agent_ledger_reader;

-- No default privileges: a table DBOS adds in a later migration must be granted
-- deliberately, not inherited by a role that predates it.
ALTER DEFAULT PRIVILEGES IN SCHEMA dbos REVOKE ALL ON TABLES FROM agent_ledger_reader;

\echo 'agent_ledger_reader provisioned. Verify with:'
\echo '  SELECT table_name, column_name, privilege_type FROM information_schema.column_privileges'
\echo '   WHERE grantee = ''agent_ledger_reader'' ORDER BY table_name, column_name;'
