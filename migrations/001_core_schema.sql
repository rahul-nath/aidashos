-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS workspaces (
  workspace_id text PRIMARY KEY,
  root_path text NOT NULL,
  tool_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  model_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingress_events (
  event_id text PRIMARY KEY,
  source_type text NOT NULL,
  source_uri text NOT NULL,
  event_type text NOT NULL,
  workspace_id text NOT NULL,
  content_sha256 text,
  detected_at timestamptz NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now(),
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'registered'
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  workflow_id text PRIMARY KEY,
  workflow_type text NOT NULL,
  workspace_id text NOT NULL REFERENCES workspaces(workspace_id),
  status text NOT NULL,
  current_stage text NOT NULL,
  input_event_id text REFERENCES ingress_events(event_id),
  retry_count int NOT NULL DEFAULT 0,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id text PRIMARY KEY,
  workflow_id text REFERENCES workflow_runs(workflow_id),
  role text NOT NULL,
  uri text NOT NULL,
  sha256 text NOT NULL,
  mime_type text NOT NULL,
  size_bytes bigint NOT NULL,
  schema_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_invocations (
  invocation_id text PRIMARY KEY,
  workflow_id text NOT NULL REFERENCES workflow_runs(workflow_id),
  model_role text NOT NULL,
  model_id text NOT NULL,
  input_artifact_id text NOT NULL,
  params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_artifact_id text,
  latency_ms int,
  status text NOT NULL,
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pi_turns (
  pi_turn_id text PRIMARY KEY,
  workflow_id text NOT NULL REFERENCES workflow_runs(workflow_id),
  workspace_id text NOT NULL,
  prompt_artifact_id text NOT NULL,
  allowed_tools_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  decision_schema text NOT NULL,
  output_artifact_id text,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id text PRIMARY KEY,
  pi_turn_id text,
  workflow_id text NOT NULL REFERENCES workflow_runs(workflow_id),
  tool_name text NOT NULL,
  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS embedding_chunks (
  chunk_id text PRIMARY KEY,
  artifact_id text NOT NULL REFERENCES artifacts(artifact_id),
  workspace_id text NOT NULL,
  chunk_index int NOT NULL,
  text_sha256 text NOT NULL,
  text text NOT NULL,
  embedding_model_id text NOT NULL,
  embedding vector,
  embedding_json jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rerank_results (
  rerank_id text PRIMARY KEY,
  query_sha256 text NOT NULL,
  candidate_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  ranked_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  model_id text NOT NULL,
  output_artifact_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS egress_writes (
  egress_id text PRIMARY KEY,
  workflow_id text NOT NULL REFERENCES workflow_runs(workflow_id),
  egress_type text NOT NULL,
  destination_uri text NOT NULL,
  content_sha256 text NOT NULL,
  request_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_json jsonb,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS egress_write_dedupe_idx
  ON egress_writes (egress_type, destination_uri, content_sha256);

CREATE INDEX IF NOT EXISTS workflow_runs_stuck_idx
  ON workflow_runs (status, current_stage, updated_at);

CREATE INDEX IF NOT EXISTS artifacts_role_created_idx
  ON artifacts (role, created_at);
