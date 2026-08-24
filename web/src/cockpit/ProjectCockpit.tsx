// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

import { RefreshCcw } from 'lucide-react'
import type { ReactNode } from 'react'
import type {
  ExecutionEventEntry,
  ExecutionFacts,
  LinkedProject,
  ProjectActionSnapshot,
} from '../api'
import { useCurrentState, useIntegrationTrigger, useLifecycleTimeline } from './lanes'

const NOT_RECORDED = '—'

function formatClockTime(iso: string | null): string {
  if (!iso) return NOT_RECORDED
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleTimeString()
}

/** Render a payload compactly: a timeline row is a summary, not a document. */
function summarizePayload(payload: Record<string, unknown>): string {
  const entries = Object.entries(payload)
  if (entries.length === 0) return ''
  return entries
    .map(([key, value]) => {
      const rendered = typeof value === 'string' ? value : JSON.stringify(value)
      return `${key}=${rendered}`
    })
    .join(' ')
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={typeof value === 'string' ? value : undefined}>{value || NOT_RECORDED}</dd>
    </div>
  )
}

/**
 * A lane whose status carries a failure reason shows both, because a bare
 * FAILED sends the operator back to the ledger to find out what failed.
 */
function LaneField({
  label,
  status,
  failure,
}: {
  label: string
  status?: string | null
  failure?: string | null
}) {
  return (
    <div className={failure ? 'laneFailed' : undefined}>
      <dt>{label}</dt>
      <dd title={failure ?? status ?? undefined}>
        {status || NOT_RECORDED}
        {failure && <span className="laneFailure">{failure}</span>}
      </dd>
    </div>
  )
}

/**
 * Render whichever execution shape the server actually sent.
 *
 * The server names the case, so this switches on it rather than guessing from the
 * presence of a lease id. An intent that has not been claimed has no agent,
 * supervisor, or persistence lane, and the previous shared panel rendered four
 * empty rows implying it did.
 */
function ExecutionPanel({ execution }: { execution: ExecutionFacts | null | undefined }) {
  if (!execution) {
    return <p className="cockpitEmpty">No execution attempt is attached to the current step.</p>
  }
  if (execution.execution_kind === 'intent') {
    return (
      <dl className="executionFacts">
        <Field label="Intent" value={execution.intent_id} />
        <Field label="Status" value={execution.status} />
        <Field label="Outcome" value={execution.outcome} />
        <Field
          label="Requested"
          value={[execution.tier, execution.kind].filter(Boolean).join(' · ')}
        />
        <Field label="Error" value={execution.error} />
        <div>
          <dt>Agent</dt>
          <dd>Not claimed yet</dd>
        </div>
      </dl>
    )
  }
  return (
    <dl className="executionFacts">
      <Field label="Lease" value={execution.lease_id} />
      <Field label="Status" value={execution.status} />
      <Field label="Outcome" value={execution.outcome} />
      <Field label="Activity" value={execution.activity_status} />
      <LaneField
        label="Agent"
        status={execution.agent_status}
        failure={execution.agent_failure ?? execution.agent_failure_category}
      />
      <LaneField
        label="Supervisor"
        status={execution.supervisor_status}
        failure={execution.supervisor_failure}
      />
      <LaneField
        label="Persistence"
        status={execution.persistence_status}
        failure={execution.persistence_failure}
      />
      <LaneField
        label="Progress assessment"
        status={execution.progress_assessment_status}
        failure={execution.progress_assessment_error}
      />
      <Field
        label="Bench slot"
        value={[execution.agent_tier, execution.agent_name].filter(Boolean).join(' · ')}
      />
      <Field label="Next action" value={execution.next_action} />
    </dl>
  )
}

function JudgementPanel({ snapshot }: { snapshot: ProjectActionSnapshot }) {
  return (
    <dl className="executionFacts">
      <Field label="Milestone" value={snapshot.milestone?.name ?? snapshot.milestone?.milestone_id} />
      <Field label="Milestone status" value={snapshot.milestone?.status} />
      <Field label="Saga" value={snapshot.saga?.status} />
      <LaneField
        label="Checkpoint"
        status={snapshot.checkpoint?.status}
        failure={snapshot.checkpoint?.error}
      />
      <Field label="Checkpoint reason" value={snapshot.checkpoint?.reason} />
      <Field
        label="Approval"
        value={[snapshot.approval?.request_type, snapshot.approval?.status]
          .filter(Boolean)
          .join(' · ')}
      />
      <Field label="Verification" value={summarizePayload(snapshot.verification ?? {})} />
      <Field label="Review" value={summarizePayload(snapshot.review ?? {})} />
      <Field
        label="Git"
        value={`${snapshot.project.branch ?? 'detached'} · ${
          snapshot.project.head_sha?.slice(0, 8) ?? 'unavailable'
        }`}
      />
    </dl>
  )
}

function TimelineRow({ event }: { event: ExecutionEventEntry }) {
  const payload = summarizePayload(event.payload)
  return (
    <li>
      <span className="timelineSequence">#{event.sequence}</span>
      <span className="timelineTime">{formatClockTime(event.occurred_at)}</span>
      <span className="timelineKind">{event.kind}</span>
      <span className="timelineSource">{event.source}</span>
      <span className="timelinePayload" title={payload}>
        {payload}
      </span>
    </li>
  )
}

export function ProjectCockpit({
  projects,
  selectedProject,
  onSelectProject,
}: {
  projects: LinkedProject[]
  selectedProject: string
  onSelectProject: (projectId: string) => void
}) {
  const currentState = useCurrentState(selectedProject)
  const integration = useIntegrationTrigger(currentState.refresh, selectedProject)
  const timeline = useLifecycleTimeline(selectedProject)
  const snapshot = currentState.data
  const approvedIntegrationId =
    snapshot?.action === 'MERGE_INTEGRATION_REQUIRED' &&
    snapshot.approval?.request_type === 'CODE_MERGE' &&
    snapshot.approval.status === 'APPROVED'
      ? snapshot.approval.approval_id
      : null
  const integrationResult =
    integration.result?.target_project_id === selectedProject ? integration.result : null

  return (
    <section className="projectCockpit" aria-label="Project action cockpit">
      <div className="projectCockpitHeader">
        <div>
          <p className="eyebrow">Authoritative next action</p>
          <h2>Project cockpit</h2>
        </div>
        <label>
          <span>Project</span>
          <select value={selectedProject} onChange={(event) => onSelectProject(event.target.value)}>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.id}
              </option>
            ))}
          </select>
        </label>
      </div>

      {snapshot ? (
        <div className="projectActionGrid">
          <div className="projectActionLead">
            <span className={`actionPill ${snapshot.action.toLowerCase()}`}>{snapshot.action}</span>
            <p>{snapshot.summary}</p>
            {snapshot.next_command && <code>{snapshot.next_command}</code>}
            {(approvedIntegrationId || integrationResult) && (
              <div className="integrationAction">
                {approvedIntegrationId &&
                  integrationResult?.state !== 'complete' &&
                  integrationResult?.state !== 'blocked' && (
                  <button
                    type="button"
                    disabled={
                      integration.pending ||
                      integrationResult?.state === 'accepted' ||
                      integrationResult?.state === 'running'
                    }
                    onClick={() => void integration.trigger(approvedIntegrationId)}
                  >
                    {integration.pending ||
                    integrationResult?.state === 'accepted' ||
                    integrationResult?.state === 'running'
                      ? 'Integrating…'
                      : 'Integrate approved work'}
                  </button>
                  )}
                {(integrationResult || integration.error) && (
                  <p role="status" className={integration.error ? 'projectActionError' : undefined}>
                    {integration.error ?? integrationResult?.message}
                  </p>
                )}
              </div>
            )}
            <p className="laneStamp" data-testid="current-state-stamp">
              Current state as of {formatClockTime(currentState.lastUpdatedAt)}
              <button
                className="iconButton"
                type="button"
                onClick={() => void currentState.refresh()}
                title="Re-read current state"
              >
                <RefreshCcw size={14} />
              </button>
            </p>
          </div>
          <div className="cockpitFactColumns">
            <ExecutionPanel execution={snapshot.execution} />
            <JudgementPanel snapshot={snapshot} />
          </div>
          {snapshot.warnings.length > 0 && (
            <ul className="projectWarnings">
              {snapshot.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="projectActionError">
          {currentState.error ?? 'Loading authoritative project state…'}
        </p>
      )}

      <div className="lifecycleTimeline" aria-label="Execution lifecycle events">
        <div className="panelHeader">
          <h3>Lifecycle events</h3>
          <span data-testid="timeline-stamp">
            {timeline.leaseId ? `lease ${timeline.leaseId.slice(0, 8)}` : 'no active lease'} ·{' '}
            {formatClockTime(timeline.lastUpdatedAt)}
          </span>
        </div>
        {timeline.error && <p className="projectActionError">{timeline.error}</p>}
        {timeline.data.length > 0 ? (
          <ol className="timelineList" data-testid="timeline-list">
            {timeline.data.map((event) => (
              <TimelineRow key={event.event_id} event={event} />
            ))}
          </ol>
        ) : (
          <p className="cockpitEmpty">
            {timeline.leaseId
              ? 'No lifecycle events recorded for this lease yet.'
              : 'No execution attempt is running for this project.'}
          </p>
        )}
        {timeline.hasMore && (
          <p className="cockpitEmpty">More history remains in the ledger beyond this page.</p>
        )}
      </div>
    </section>
  )
}
