-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- Agent coordination ledger schema (Postgres).
--
-- The test-only SQLite adapter mirrors this command surface under
-- potential_directions/sqlite_test_adapter/ so agent_coordination_mcp.py can
-- keep one public CLI/MCP contract while runtime truth remains in Postgres.
-- JSON payload columns stay text in this first migration slice to preserve the
-- existing command behavior and avoid changing every call site at once.

CREATE TABLE IF NOT EXISTS coordination_schema_versions (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    applied_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    last_heartbeat_at DOUBLE PRECISION NOT NULL
);

-- The `claims` table was removed at schema version 17. Its code is archived in
-- potential_directions/file_claims/, and the reasons are there: no dispatched
-- agent could reach it, the file set is unknowable at dispatch time, and its
-- path primary key collided across projects. It is not dropped from databases
-- that already have it, because an unused table costs nothing and dropping
-- rows is not something a schema bump should do behind an operator.

CREATE TABLE IF NOT EXISTS notes (
    id BIGSERIAL PRIMARY KEY,
    scope TEXT NOT NULL,
    session_id TEXT,
    agent_name TEXT,
    message TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_scope_created ON notes(scope, created_at);

CREATE TABLE IF NOT EXISTS handoffs (
    id BIGSERIAL PRIMARY KEY,
    paths_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    agent_name TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoffs_created_at ON handoffs(created_at);

CREATE TABLE IF NOT EXISTS gawd_docs (
    gawd_doc_id TEXT PRIMARY KEY,
    saga_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    goal TEXT NOT NULL,
    constraints_json TEXT NOT NULL DEFAULT '[]',
    success_criteria_json TEXT NOT NULL DEFAULT '[]',
    unresolved_questions_json TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    task_graph_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    approved_at DOUBLE PRECISION,
    superseded_by TEXT REFERENCES gawd_docs(gawd_doc_id),
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gawd_docs_saga ON gawd_docs(saga_id);

CREATE TABLE IF NOT EXISTS sagas (
    saga_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    gawd_doc_id TEXT REFERENCES gawd_docs(gawd_doc_id),
    current_stage TEXT NOT NULL DEFAULT 'IDEA_INTAKE',
    status TEXT NOT NULL DEFAULT 'PLANNING',
    budget_tokens BIGINT NOT NULL DEFAULT 1000000,
    consumed_tokens BIGINT NOT NULL DEFAULT 0,
    budget_seconds INTEGER NOT NULL DEFAULT 86400,
    tokens_used BIGINT NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_sagas_status ON sagas(status);
ALTER TABLE sagas ADD COLUMN IF NOT EXISTS consumed_tokens BIGINT NOT NULL DEFAULT 0;

-- Intake dedupe. A repeated ingest of one draft must replay onto the existing
-- saga rather than create a second one; the unique index is what makes the
-- duplicate unrepresentable instead of merely unlikely. NULL is exempt, so
-- sagas created by any other path are unaffected.
ALTER TABLE sagas ADD COLUMN IF NOT EXISTS content_digest TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sagas_content_digest
    ON sagas(content_digest) WHERE content_digest IS NOT NULL;

CREATE TABLE IF NOT EXISTS saga_milestones (
    milestone_id TEXT PRIMARY KEY,
    saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
    gawd_doc_id TEXT REFERENCES gawd_docs(gawd_doc_id),
    sequence INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    entry_criteria_json TEXT NOT NULL DEFAULT '[]',
    exit_criteria_json TEXT NOT NULL DEFAULT '[]',
    required_artifacts_json TEXT NOT NULL DEFAULT '[]',
    approval_required INTEGER NOT NULL DEFAULT 0,
    dispatch_intent_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    outcome TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,
    completed_at DOUBLE PRECISION
);
ALTER TABLE saga_milestones ADD COLUMN IF NOT EXISTS outcome TEXT;
CREATE INDEX IF NOT EXISTS idx_saga_milestones_saga
    ON saga_milestones(saga_id, sequence);
CREATE INDEX IF NOT EXISTS idx_saga_milestones_status
    ON saga_milestones(status, sequence);

CREATE TABLE IF NOT EXISTS milestone_evidence (
    evidence_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES saga_milestones(milestone_id),
    saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
    evidence_type TEXT NOT NULL,
    content TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'milestone_evidence.v1',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_milestone_evidence_milestone
    ON milestone_evidence(milestone_id, created_at);

CREATE TABLE IF NOT EXISTS pow_wows (
    pow_wow_id TEXT PRIMARY KEY,
    saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
    stage TEXT NOT NULL,
    goal TEXT NOT NULL,
    input_artifacts_json TEXT NOT NULL DEFAULT '[]',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    budget_tokens BIGINT NOT NULL DEFAULT 100000,
    consumed_tokens BIGINT NOT NULL DEFAULT 0,
    exit_criteria TEXT NOT NULL DEFAULT '',
    required_outputs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'FORMING',
    output_summary TEXT,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_pow_wows_saga ON pow_wows(saga_id);
CREATE INDEX IF NOT EXISTS idx_pow_wows_status ON pow_wows(status);
ALTER TABLE pow_wows ADD COLUMN IF NOT EXISTS consumed_tokens BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS pow_wow_agents (
    id BIGSERIAL PRIMARY KEY,
    pow_wow_id TEXT NOT NULL REFERENCES pow_wows(pow_wow_id),
    session_id TEXT REFERENCES sessions(session_id),
    agent_name TEXT NOT NULL,
    role TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    joined_at DOUBLE PRECISION NOT NULL,
    UNIQUE (pow_wow_id, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_pow_wow_agents_pw ON pow_wow_agents(pow_wow_id);

CREATE TABLE IF NOT EXISTS saga_tasks (
    task_id TEXT PRIMARY KEY,
    pow_wow_id TEXT NOT NULL REFERENCES pow_wows(pow_wow_id),
    saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
    task_name TEXT NOT NULL,
    description TEXT NOT NULL,
    assigned_session_id TEXT REFERENCES sessions(session_id),
    assigned_agent_name TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    blocked_by_json TEXT NOT NULL DEFAULT '[]',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_saga_tasks_pw ON saga_tasks(pow_wow_id);
CREATE INDEX IF NOT EXISTS idx_saga_tasks_status ON saga_tasks(status);

CREATE TABLE IF NOT EXISTS task_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES saga_tasks(task_id),
    pow_wow_id TEXT NOT NULL REFERENCES pow_wows(pow_wow_id),
    saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
    artifact_type TEXT NOT NULL,
    content TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'v1',
    submitted_by_session TEXT REFERENCES sessions(session_id),
    submitted_by_agent TEXT,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    evaluation_score DOUBLE PRECISION,
    evaluation_status TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_pw ON task_artifacts(pow_wow_id);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id);

CREATE TABLE IF NOT EXISTS tool_permission_requests (
    request_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    agent_name TEXT NOT NULL,
    task_id TEXT REFERENCES saga_tasks(task_id),
    pow_wow_id TEXT REFERENCES pow_wows(pow_wow_id),
    tool_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    granted_by TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION,
    -- When this grant stops granting. NULL means it does not expire, which is
    -- what every row written before this column existed meant implicitly and
    -- what a deliberate standing grant means explicitly. The difference is that
    -- one of those is now a choice somebody made.
    --
    -- A grant is a statement about a piece of work, and work ends. A GRANTED row
    -- that outlives its pow-wow is an authorization nobody remembers making and
    -- nobody will think to remove.
    expires_at DOUBLE PRECISION
);
ALTER TABLE tool_permission_requests ADD COLUMN IF NOT EXISTS expires_at DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_tool_perms_expiry
    ON tool_permission_requests(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_tool_perms_session
    ON tool_permission_requests(session_id, status);
CREATE INDEX IF NOT EXISTS idx_tool_perms_agent
    ON tool_permission_requests(agent_name, status);

CREATE TABLE IF NOT EXISTS evaluation_results (
    eval_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES task_artifacts(artifact_id),
    pow_wow_id TEXT NOT NULL REFERENCES pow_wows(pow_wow_id),
    evaluator_session_id TEXT REFERENCES sessions(session_id),
    evaluator_agent_name TEXT,
    eval_type TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    passed INTEGER NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evals_artifact ON evaluation_results(artifact_id);
CREATE INDEX IF NOT EXISTS idx_evals_pow_wow ON evaluation_results(pow_wow_id);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
    request_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    requested_by TEXT,
    resolved_by TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_approvals_saga ON approval_requests(saga_id, status);

CREATE TABLE IF NOT EXISTS drift_checks (
    check_id TEXT PRIMARY KEY,
    pow_wow_id TEXT NOT NULL REFERENCES pow_wows(pow_wow_id),
    gawd_doc_id TEXT NOT NULL REFERENCES gawd_docs(gawd_doc_id),
    drift_detected INTEGER NOT NULL DEFAULT 0,
    drift_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    drift_reasons_json TEXT NOT NULL DEFAULT '[]',
    checked_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drift_checks_pw ON drift_checks(pow_wow_id);

CREATE TABLE IF NOT EXISTS dispatch_intents (
    intent_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'advisory',
    prompt TEXT NOT NULL,
    target_project_id TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    claimed_by TEXT,
    result TEXT,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    claimed_at DOUBLE PRECISION,
    completed_at DOUBLE PRECISION,
    fanout INTEGER NOT NULL DEFAULT 1,
    allow_tiers TEXT NOT NULL DEFAULT '[]',
    reduce TEXT NOT NULL DEFAULT 'none',
    reducer_tier TEXT,
    parent_intent_id TEXT,
    intent_role TEXT NOT NULL DEFAULT 'single',
    checkpoint_id TEXT,
    outcome TEXT,
    -- What a process spawned for this intent may do, as a JSON array of
    -- `Capability` values. The compiled plan computes this per milestone and it
    -- used to stop at the agent's prompt: the spawn decision was made from a
    -- boolean derived from the task's name, and every task that boolean called
    -- not-a-review was launched with the sandbox turned off.
    -- Defaulted to '[]' rather than to a permissive set, because an intent
    -- submitted before this column existed has declared nothing, and nothing
    -- must read as the narrowest authority.
    permitted_capabilities TEXT NOT NULL DEFAULT '[]'
);
ALTER TABLE dispatch_intents
    ADD COLUMN IF NOT EXISTS permitted_capabilities TEXT NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS idx_dispatch_intents_status
    ON dispatch_intents(status, created_at);
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS fanout INTEGER NOT NULL DEFAULT 1;
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS allow_tiers TEXT NOT NULL DEFAULT '[]';
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS reduce TEXT NOT NULL DEFAULT 'none';
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS reducer_tier TEXT;
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS parent_intent_id TEXT;
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS intent_role TEXT NOT NULL DEFAULT 'single';
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS checkpoint_id TEXT;
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS outcome TEXT;
-- Nullable, and unique only where present. A producer that has no natural
-- identity for its request (an operator typing `pi /saga`) supplies nothing and
-- two such requests are two intents, which is correct: they were two asks.
-- A producer whose request IS identified by durable state -- milestone M of
-- WorkUnit W on attempt N -- supplies that identity, and the second submit of
-- it returns the first intent instead of a second agent doing the same work.
-- NULLs do not collide in a unique index in either Postgres or SQLite, so every
-- row written before this column existed stays legal.
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_intents_idempotency_key
    ON dispatch_intents(idempotency_key);
-- The DBOS workflow to wake when this intent settles. NULL for producers with
-- nobody waiting (an ASR trigger, an operator at a terminal); a milestone that
-- parked on `DBOS.recv` records the workflow id that must be sent to.
ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS notify_workflow_id TEXT;
CREATE INDEX IF NOT EXISTS idx_dispatch_intents_parent
    ON dispatch_intents(parent_intent_id);

CREATE TABLE IF NOT EXISTS agent_execution_leases (
    lease_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    intent_id TEXT REFERENCES dispatch_intents(intent_id),
    task_id TEXT REFERENCES saga_tasks(task_id),
    worker_id TEXT NOT NULL,
    agent_tier TEXT,
    agent_name TEXT,
    worktree_path TEXT,
    command_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    outcome TEXT,
    agent_status TEXT NOT NULL DEFAULT 'PENDING',
    agent_failure_category TEXT,
    agent_failure TEXT,
    supervisor_status TEXT NOT NULL DEFAULT 'PENDING',
    supervisor_failure TEXT,
    persistence_status TEXT NOT NULL DEFAULT 'PENDING',
    persistence_failure TEXT,
    next_action TEXT,
    activity_status TEXT NOT NULL DEFAULT 'STARTING',
    last_meaningful_progress_at DOUBLE PRECISION,
    last_meaningful_progress_sequence INTEGER,
    progress_assessment_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
    progress_assessment_decision_json TEXT,
    progress_assessment_error TEXT,
    progress_assessed_at DOUBLE PRECISION,
    timeout_seconds INTEGER NOT NULL DEFAULT 3600,
    lease_expires_at DOUBLE PRECISION NOT NULL,
    cancel_requested_at DOUBLE PRECISION,
    compensation_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    heartbeat_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION
);
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS agent_status TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS agent_failure_category TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS agent_failure TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS supervisor_status TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS supervisor_failure TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS persistence_status TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS persistence_failure TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS next_action TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS activity_status TEXT NOT NULL DEFAULT 'STARTING';
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS last_meaningful_progress_at DOUBLE PRECISION;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS last_meaningful_progress_sequence INTEGER;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS progress_assessment_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED';
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS progress_assessment_decision_json TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS progress_assessment_error TEXT;
ALTER TABLE agent_execution_leases ADD COLUMN IF NOT EXISTS progress_assessed_at DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_agent_execution_leases_status
    ON agent_execution_leases(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_execution_leases_intent
    ON agent_execution_leases(intent_id);

CREATE TABLE IF NOT EXISTS agent_execution_events (
    event_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL REFERENCES agent_execution_leases(lease_id),
    sequence INTEGER NOT NULL,
    occurred_at DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_sha256 TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(lease_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_agent_execution_events_lease
    ON agent_execution_events(lease_id, sequence);

CREATE TABLE IF NOT EXISTS agent_execution_artifacts (
    execution_artifact_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL REFERENCES agent_execution_leases(lease_id),
    artifact_id TEXT NOT NULL,
    role TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(lease_id, artifact_id, role)
);
CREATE INDEX IF NOT EXISTS idx_agent_execution_artifacts_lease
    ON agent_execution_artifacts(lease_id, created_at);

CREATE TABLE IF NOT EXISTS agent_execution_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL UNIQUE REFERENCES agent_execution_leases(lease_id),
    intent_id TEXT REFERENCES dispatch_intents(intent_id),
    saga_id TEXT,
    pow_wow_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    worktree_path TEXT,
    source_repo_path TEXT,
    base_head_sha TEXT,
    transcript_artifact_id TEXT,
    patch_artifact_id TEXT,
    git_status_artifact_id TEXT,
    test_summary_artifact_id TEXT,
    junior_review_artifact_id TEXT,
    review_intent_id TEXT REFERENCES dispatch_intents(intent_id),
    decision_json TEXT,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    decided_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_agent_execution_checkpoints_status
    ON agent_execution_checkpoints(status, created_at);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL DEFAULT '',
    aggregate_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT,
    error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    claimed_at DOUBLE PRECISION,
    processed_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_ledger_events_status
    ON ledger_events(status, created_at);

-- Monitor feedback reactor (docs/monitor_feedback_loop_design.md).
-- One row per evaluated signal, including every suppression: "the loop chose
-- not to act" is the most common operator question and therefore evidence.
CREATE TABLE IF NOT EXISTS monitor_feedback_events (
    feedback_event_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    signal_source TEXT NOT NULL,
    signal_kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    target_project_id TEXT,
    rule_id TEXT,
    decision TEXT NOT NULL,
    intent_id TEXT,
    approval_id TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    observed_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_feedback_events_fingerprint
    ON monitor_feedback_events(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_monitor_feedback_events_rule
    ON monitor_feedback_events(rule_id, created_at);

-- One watermark per fact kind, advanced in the same transaction that commits
-- the decision rows. A crashed cycle re-evaluates rather than drops, and
-- re-evaluation is harmless because the fingerprint dedup absorbs it.
CREATE TABLE IF NOT EXISTS monitor_feedback_watermarks (
    signal_kind TEXT PRIMARY KEY,
    observed_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

-- ----------------------------------------------------------------
-- DesignDoc-governed WorkUnit execution
--
-- One WorkUnit is one root DBOS workflow execution. These tables are the
-- application-owned domain model: DBOS owns durable execution checkpoints, and
-- everything an operator or the cockpit needs to understand the work lives here.
--
-- The revision tables are immutable by contract. Nothing in the package issues
-- an UPDATE against design_doc_revisions, compiled_plan_revisions,
-- compiled_milestones, milestone_dependencies, or work_unit_events; a material
-- change creates a new revision and a new WorkUnit that supersedes the old one.
-- ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS design_doc_revisions (
    design_doc_revision_id TEXT PRIMARY KEY,
    design_doc_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    structured_content TEXT,
    source_path TEXT,
    schema_version TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_design_doc_revisions_number
    ON design_doc_revisions(design_doc_id, revision_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_design_doc_revisions_content
    ON design_doc_revisions(design_doc_id, content_hash);

CREATE TABLE IF NOT EXISTS compiled_plan_revisions (
    compiled_plan_revision_id TEXT PRIMARY KEY,
    work_unit_id TEXT,
    design_doc_revision_id TEXT NOT NULL
        REFERENCES design_doc_revisions(design_doc_revision_id),
    compiler_version TEXT NOT NULL,
    lifecycle_profile TEXT NOT NULL,
    lifecycle_profile_version INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_errors TEXT NOT NULL DEFAULT '[]',
    execution_blockers TEXT NOT NULL DEFAULT '[]',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compiled_plan_revisions_source
    ON compiled_plan_revisions(design_doc_revision_id, created_at);

CREATE TABLE IF NOT EXISTS compiled_milestones (
    milestone_id TEXT PRIMARY KEY,
    compiled_plan_revision_id TEXT NOT NULL
        REFERENCES compiled_plan_revisions(compiled_plan_revision_id),
    stable_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    executor_kind TEXT NOT NULL,
    requires_operator_approval INTEGER NOT NULL DEFAULT 0,
    source_start INTEGER NOT NULL,
    source_end INTEGER NOT NULL,
    source_heading TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_compiled_milestones_key
    ON compiled_milestones(compiled_plan_revision_id, stable_key);
CREATE INDEX IF NOT EXISTS idx_compiled_milestones_phase
    ON compiled_milestones(compiled_plan_revision_id, phase, ordinal);

CREATE TABLE IF NOT EXISTS milestone_dependencies (
    compiled_plan_revision_id TEXT NOT NULL
        REFERENCES compiled_plan_revisions(compiled_plan_revision_id),
    milestone_id TEXT NOT NULL REFERENCES compiled_milestones(milestone_id),
    depends_on_milestone_id TEXT NOT NULL REFERENCES compiled_milestones(milestone_id),
    PRIMARY KEY (milestone_id, depends_on_milestone_id),
    CHECK (milestone_id <> depends_on_milestone_id)
);
CREATE INDEX IF NOT EXISTS idx_milestone_dependencies_plan
    ON milestone_dependencies(compiled_plan_revision_id);

CREATE TABLE IF NOT EXISTS work_units (
    work_unit_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    current_phase TEXT NOT NULL,
    design_doc_revision_id TEXT NOT NULL
        REFERENCES design_doc_revisions(design_doc_revision_id),
    compiled_plan_revision_id TEXT NOT NULL
        REFERENCES compiled_plan_revisions(compiled_plan_revision_id),
    compiled_plan_hash TEXT NOT NULL,
    lifecycle_profile TEXT NOT NULL,
    lifecycle_profile_version INTEGER NOT NULL,
    root_workflow_id TEXT NOT NULL,
    supersedes_work_unit_id TEXT REFERENCES work_units(work_unit_id),
    legacy_saga_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,
    completed_at DOUBLE PRECISION,
    blocked_at DOUBLE PRECISION,
    failure_code TEXT,
    failure_summary TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_units_root_workflow
    ON work_units(root_workflow_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_units_legacy_saga
    ON work_units(legacy_saga_id) WHERE legacy_saga_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_units_status
    ON work_units(status, created_at);

CREATE TABLE IF NOT EXISTS milestone_executions (
    milestone_execution_id TEXT PRIMARY KEY,
    work_unit_id TEXT NOT NULL REFERENCES work_units(work_unit_id),
    milestone_id TEXT NOT NULL REFERENCES compiled_milestones(milestone_id),
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    child_workflow_id TEXT,
    dispatch_intent_id TEXT,
    started_at DOUBLE PRECISION,
    completed_at DOUBLE PRECISION,
    blocked_at DOUBLE PRECISION,
    failure_code TEXT,
    failure_summary TEXT,
    -- How the failure must be handled, kept beside the code that names it.
    -- `failure_code` is free text written in four places; `failure_class` is the
    -- closed `FailureClass` vocabulary, and it is the only thing that answers
    -- "did this BLOCKED milestone spend an attempt?". Without it a retry budget
    -- has to guess from a string, and guessing wrong on an approval gate fails a
    -- milestone that never ran.
    failure_class TEXT,
    result_summary TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
ALTER TABLE milestone_executions ADD COLUMN IF NOT EXISTS failure_class TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_milestone_executions_unique
    ON milestone_executions(work_unit_id, milestone_id);
CREATE INDEX IF NOT EXISTS idx_milestone_executions_status
    ON milestone_executions(work_unit_id, status);

CREATE TABLE IF NOT EXISTS work_unit_events (
    event_id TEXT PRIMARY KEY,
    work_unit_id TEXT NOT NULL REFERENCES work_units(work_unit_id),
    sequence_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT,
    milestone_execution_id TEXT REFERENCES milestone_executions(milestone_execution_id),
    root_workflow_id TEXT NOT NULL,
    child_workflow_id TEXT,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_unit_events_sequence
    ON work_unit_events(work_unit_id, sequence_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_unit_events_idempotency
    ON work_unit_events(idempotency_key);

CREATE TABLE IF NOT EXISTS work_unit_artifacts (
    artifact_id TEXT PRIMARY KEY,
    work_unit_id TEXT NOT NULL REFERENCES work_units(work_unit_id),
    milestone_execution_id TEXT REFERENCES milestone_executions(milestone_execution_id),
    artifact_type TEXT NOT NULL,
    uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT,
    size_bytes BIGINT,
    producer_workflow_id TEXT NOT NULL,
    producer_step_name TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_unit_artifacts_identity
    ON work_unit_artifacts(work_unit_id, artifact_type, content_hash);
CREATE INDEX IF NOT EXISTS idx_work_unit_artifacts_milestone
    ON work_unit_artifacts(milestone_execution_id);

-- One row per operator decision the lifecycle is waiting on. An approval is a
-- durable request that names exactly what it authorizes; a chat message that
-- does not resolve a named request cannot unblock anything.
CREATE TABLE IF NOT EXISTS work_unit_decision_requests (
    request_id TEXT PRIMARY KEY,
    work_unit_id TEXT NOT NULL REFERENCES work_units(work_unit_id),
    milestone_execution_id TEXT REFERENCES milestone_executions(milestone_execution_id),
    request_kind TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    decision_payload_json TEXT,
    decided_by TEXT,
    response_idempotency_key TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_unit_decision_requests_response
    ON work_unit_decision_requests(response_idempotency_key)
    WHERE response_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_unit_decision_requests_status
    ON work_unit_decision_requests(work_unit_id, status);

-- The enqueue outbox exists because the coordination ledger and the DBOS system
-- database are separate physical databases. A WorkUnit row and its intent to
-- start are committed together here; an idempotent dispatcher then hands the
-- explicit workflow ID to DBOS and marks the row delivered only afterwards.
CREATE TABLE IF NOT EXISTS work_unit_enqueue_outbox (
    outbox_id TEXT PRIMARY KEY,
    work_unit_id TEXT NOT NULL REFERENCES work_units(work_unit_id),
    root_workflow_id TEXT NOT NULL,
    design_doc_revision_id TEXT NOT NULL,
    compiled_plan_revision_id TEXT NOT NULL,
    compiled_plan_hash TEXT NOT NULL,
    lifecycle_profile_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    delivered_at DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_unit_enqueue_outbox_unit
    ON work_unit_enqueue_outbox(work_unit_id);
CREATE INDEX IF NOT EXISTS idx_work_unit_enqueue_outbox_status
    ON work_unit_enqueue_outbox(status, created_at);

-- The refinery's queue: which approved agent branches are waiting to land, and
-- what happened to the ones that no longer are.
--
-- The columns are the request's immutable subject, and the two mutable ones are
-- its state and that state's own payload. That split is the datatype in
-- `refinery/requests.py` written down: a subject is fixed for the request's
-- whole life and `require_integration_transition` refuses a transition that
-- changes it, while the five states carry fields that are not optional versions
-- of each other. A landed request has an integration commit and no cause, a
-- bisected one has a cause and no integration commit, and a queued one has
-- neither and no batch either. Twelve nullable columns would have to be read
-- together to know which three of them mean anything, so the variant's fields
-- live in one JSON payload behind the discriminator that says how to read it.
--
-- Nothing queries the payload. Every question the queue is asked - what is
-- waiting for this project, does this commit already have a live request, which
-- request did this approval produce - is answered by a subject column or by
-- `state`, which is why those are columns and the rest is not.
CREATE TABLE IF NOT EXISTS integration_requests (
    request_id TEXT PRIMARY KEY,
    target_project_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    base_head_sha TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    approval_id TEXT NOT NULL REFERENCES approval_requests(approval_id),
    intent_id TEXT NOT NULL,
    pow_wow_id TEXT NOT NULL,
    milestone_key TEXT,
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    enqueued_at DOUBLE PRECISION NOT NULL,
    state TEXT NOT NULL,
    state_payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at DOUBLE PRECISION NOT NULL
);
-- Idempotent enqueue, as a constraint rather than as a check the caller
-- remembers to run. One commit may have at most one request that has not yet
-- been decided; a replayed resolution therefore cannot produce a second, and two
-- resolutions racing cannot both win. Partial, because a commit that was
-- bisected out and later revised and re-approved has to be able to queue again.
CREATE UNIQUE INDEX IF NOT EXISTS idx_integration_requests_live_commit
    ON integration_requests(target_project_id, commit_sha)
    WHERE state IN ('QUEUED', 'IN_FLIGHT');
CREATE INDEX IF NOT EXISTS idx_integration_requests_queue
    ON integration_requests(target_project_id, state, enqueued_at, request_id);
CREATE INDEX IF NOT EXISTS idx_integration_requests_approval
    ON integration_requests(approval_id);
