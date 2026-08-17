-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- Saga-level durable multi-agent workflows
CREATE TABLE IF NOT EXISTS sagas (
  saga_id        text PRIMARY KEY,
  goal           text NOT NULL,
  current_stage  text NOT NULL DEFAULT 'IDEA_INTAKE',
  status         text NOT NULL DEFAULT 'PLANNING',
  budget_tokens  bigint NOT NULL DEFAULT 1000000,
  consumed_tokens bigint NOT NULL DEFAULT 0,
  budget_seconds int    NOT NULL DEFAULT 86400,
  tokens_used    bigint NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz
);

-- GAWD docs: immutable spec artifacts; changes create a new version
CREATE TABLE IF NOT EXISTS gawd_docs (
  gawd_doc_id              text PRIMARY KEY,
  saga_id                  text REFERENCES sagas(saga_id),
  version                  int  NOT NULL DEFAULT 1,
  goal                     text NOT NULL,
  constraints_json         jsonb NOT NULL DEFAULT '[]',
  success_criteria_json    jsonb NOT NULL DEFAULT '[]',
  unresolved_questions_json jsonb NOT NULL DEFAULT '[]',
  acceptance_criteria_json jsonb NOT NULL DEFAULT '[]',
  task_graph_json          jsonb NOT NULL DEFAULT '{}',
  status                   text NOT NULL DEFAULT 'DRAFT',  -- DRAFT | APPROVED | SUPERSEDED
  approved_at              timestamptz,
  superseded_by            text REFERENCES gawd_docs(gawd_doc_id),
  created_at               timestamptz NOT NULL DEFAULT now()
);

-- Pow-wows: bounded multi-agent stages within a saga
CREATE TABLE IF NOT EXISTS pow_wows (
  pow_wow_id          text PRIMARY KEY,
  saga_id             text NOT NULL REFERENCES sagas(saga_id),
  stage               text NOT NULL,
  goal                text NOT NULL,
  input_artifacts_json  jsonb NOT NULL DEFAULT '[]',
  allowed_tools_json    jsonb NOT NULL DEFAULT '[]',
  budget_tokens         bigint NOT NULL DEFAULT 100000,
  consumed_tokens       bigint NOT NULL DEFAULT 0,
  exit_criteria         text NOT NULL DEFAULT '',
  required_outputs_json jsonb NOT NULL DEFAULT '[]',
  status                text NOT NULL DEFAULT 'FORMING',  -- FORMING | ACTIVE | EVALUATING | COMPLETED | FAILED
  output_summary        text,
  cycle_count           int  NOT NULL DEFAULT 0,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  completed_at          timestamptz
);

-- Agent enrollment in a pow-wow (role ≠ permissions)
CREATE TABLE IF NOT EXISTS pow_wow_agents (
  id              bigserial PRIMARY KEY,
  pow_wow_id      text NOT NULL REFERENCES pow_wows(pow_wow_id),
  agent_name      text NOT NULL,
  role            text NOT NULL,
  allowed_tools_json jsonb NOT NULL DEFAULT '[]',
  status          text NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | COMPLETED | DROPPED
  joined_at       timestamptz NOT NULL DEFAULT now()
);

-- Tasks: discrete work units within a pow-wow
CREATE TABLE IF NOT EXISTS saga_tasks (
  task_id              text PRIMARY KEY,
  pow_wow_id           text NOT NULL REFERENCES pow_wows(pow_wow_id),
  saga_id              text NOT NULL REFERENCES sagas(saga_id),
  task_name            text NOT NULL,
  description          text NOT NULL,
  assigned_agent       text,
  status               text NOT NULL DEFAULT 'PENDING',  -- PENDING | CLAIMED | IN_PROGRESS | COMPLETED | FAILED | BLOCKED
  blocked_by_json      jsonb NOT NULL DEFAULT '[]',
  retry_count          int  NOT NULL DEFAULT 0,
  max_retries          int  NOT NULL DEFAULT 3,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  completed_at         timestamptz
);

-- Artifacts produced by tasks inside pow-wows
CREATE TABLE IF NOT EXISTS saga_artifacts (
  artifact_id          text PRIMARY KEY,
  task_id              text REFERENCES saga_tasks(task_id),
  pow_wow_id           text NOT NULL REFERENCES pow_wows(pow_wow_id),
  saga_id              text NOT NULL REFERENCES sagas(saga_id),
  artifact_type        text NOT NULL,
  content_uri          text NOT NULL,
  schema_version       text NOT NULL DEFAULT 'v1',
  submitted_by         text,
  evaluation_score     real,
  evaluation_status    text,   -- NULL | PENDING | PASSED | FAILED
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- Explicit tool-permission grants (runtime, not role-derived)
CREATE TABLE IF NOT EXISTS tool_permission_grants (
  grant_id      text PRIMARY KEY,
  agent_name    text NOT NULL,
  task_id       text REFERENCES saga_tasks(task_id),
  pow_wow_id    text REFERENCES pow_wows(pow_wow_id),
  tool_name     text NOT NULL,
  reason        text NOT NULL,
  status        text NOT NULL DEFAULT 'PENDING',  -- PENDING | GRANTED | DENIED
  granted_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at   timestamptz
);

-- Evaluation results: mechanical / semantic / consensus
CREATE TABLE IF NOT EXISTS saga_evaluation_results (
  eval_id              text PRIMARY KEY,
  artifact_id          text NOT NULL REFERENCES saga_artifacts(artifact_id),
  pow_wow_id           text NOT NULL REFERENCES pow_wows(pow_wow_id),
  evaluator_agent      text,
  eval_type            text NOT NULL,   -- MECHANICAL | SEMANTIC | CONSENSUS
  score                real NOT NULL,   -- 0.0–1.0
  passed               boolean NOT NULL,
  notes                text NOT NULL DEFAULT '',
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- User-approval gates (purchase, external comms, code merge, model escalation)
CREATE TABLE IF NOT EXISTS saga_approval_requests (
  approval_id    text PRIMARY KEY,
  saga_id        text NOT NULL REFERENCES sagas(saga_id),
  request_type   text NOT NULL,   -- PURCHASE | EXTERNAL_COMMS | CODE_MERGE | MODEL_ESCALATION | GENERAL
  payload_json   jsonb NOT NULL DEFAULT '{}',
  status         text NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | DENIED
  requested_by   text,
  resolved_by    text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  resolved_at    timestamptz
);

-- Drift checks: per-pow-wow comparison against GAWD doc
CREATE TABLE IF NOT EXISTS drift_checks (
  check_id           text PRIMARY KEY,
  pow_wow_id         text NOT NULL REFERENCES pow_wows(pow_wow_id),
  gawd_doc_id        text NOT NULL REFERENCES gawd_docs(gawd_doc_id),
  drift_detected     boolean NOT NULL DEFAULT false,
  drift_score        real    NOT NULL DEFAULT 0.0,
  drift_reasons_json jsonb   NOT NULL DEFAULT '[]',
  checked_at         timestamptz NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS sagas_status_idx              ON sagas(status);
CREATE INDEX IF NOT EXISTS pow_wows_saga_id_idx          ON pow_wows(saga_id);
CREATE INDEX IF NOT EXISTS pow_wows_status_idx           ON pow_wows(status);
CREATE INDEX IF NOT EXISTS pow_wow_agents_pow_wow_idx    ON pow_wow_agents(pow_wow_id);
CREATE INDEX IF NOT EXISTS saga_tasks_pow_wow_idx        ON saga_tasks(pow_wow_id);
CREATE INDEX IF NOT EXISTS saga_tasks_status_idx         ON saga_tasks(status);
CREATE INDEX IF NOT EXISTS saga_artifacts_pow_wow_idx    ON saga_artifacts(pow_wow_id);
CREATE INDEX IF NOT EXISTS gawd_docs_saga_idx            ON gawd_docs(saga_id);
CREATE INDEX IF NOT EXISTS tool_grants_agent_idx         ON tool_permission_grants(agent_name, status);
CREATE INDEX IF NOT EXISTS evals_artifact_idx            ON saga_evaluation_results(artifact_id);
CREATE INDEX IF NOT EXISTS evals_pow_wow_idx             ON saga_evaluation_results(pow_wow_id);
CREATE INDEX IF NOT EXISTS approval_requests_saga_idx    ON saga_approval_requests(saga_id, status);
CREATE INDEX IF NOT EXISTS drift_checks_pow_wow_idx      ON drift_checks(pow_wow_id);
