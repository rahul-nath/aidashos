SELECT 'CREATE DATABASE local_agent_dbos'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'local_agent_dbos')\gexec

\connect local_agent
CREATE EXTENSION IF NOT EXISTS vector;

\connect local_agent_dbos
CREATE EXTENSION IF NOT EXISTS vector;
