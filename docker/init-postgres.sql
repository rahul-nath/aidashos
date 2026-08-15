SELECT 'CREATE DATABASE local_agent_dbos'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'local_agent_dbos')\gexec

-- The vector extension is provisioning for embeddings work that has not landed.
-- Measured 2026-08-03: no table in either database has a column of type `vector`,
-- and nothing under `src/` names one. Kept because creating it later on a
-- populated database is a migration, while creating it now costs nothing.
--
-- The consequence worth knowing: the test suite does not need `pgvector/pgvector`
-- and runs on any stock Postgres 16. Recorded here rather than in a handoff,
-- because this file is where someone would come to change it.
\connect local_agent
CREATE EXTENSION IF NOT EXISTS vector;

\connect local_agent_dbos
CREATE EXTENSION IF NOT EXISTS vector;
