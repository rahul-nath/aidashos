// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The WorkUnit cockpit: durable lifecycle state, and the one action to take.
 *
 * What an operator needs from a governed WorkUnit is which phase it is in, which
 * milestones exist and how they went, what is blocking, what decision is pending,
 * and what evidence was produced. Model activity is not the abstraction; none of
 * this renders a transcript.
 *
 * Every type here comes from `../api`, generated from the server's own schema. A
 * renamed field or a new status member is a build failure rather than an empty
 * cell.
 */

import { CheckCircle2, RefreshCcw, ShieldQuestion, XCircle } from 'lucide-react'
import { Fragment, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { RuntimeControls } from './RuntimeControls'

import type {
  ArtifactView,
  BlockingCondition,
  EventView,
  MilestoneView,
  NextCommandSet,
  PhaseView,
  StatusLegendEntry,
  StatusLegendView,
  WorkUnitSummary,
  WorkUnitView,
} from '../api'
import {
  useDecisionSubmission,
  useStatusLegend,
  useWorkUnit,
  useWorkUnitEvents,
  useWorkUnitList,
  useWorkUnitNextCommands,
} from './lanes'

const NOT_RECORDED = '—'

/**
 * The server's legend, keyed for tooltip lookup.
 *
 * The arrays keep the server's declaration order for the expandable panel; the
 * maps serve the per-token tooltips. Both orders and all wording come from
 * `/status-legend`, so a status the server gains arrives here with its
 * explanation instead of as a bare pill.
 */
type LegendLookup = {
  view: StatusLegendView
  workUnit: Map<string, StatusLegendEntry>
  phase: Map<string, StatusLegendEntry>
  milestone: Map<string, StatusLegendEntry>
  dispatch: Map<string, StatusLegendEntry>
}

function keyedByStatus(entries: StatusLegendEntry[]): Map<string, StatusLegendEntry> {
  return new Map(entries.map((entry) => [entry.status, entry]))
}

function useLegendLookup(legend: StatusLegendView | null): LegendLookup | null {
  return useMemo(() => {
    if (!legend) return null
    return {
      view: legend,
      workUnit: keyedByStatus(legend.work_unit),
      phase: keyedByStatus(legend.phase),
      milestone: keyedByStatus(legend.milestone),
      dispatch: keyedByStatus(legend.dispatch),
    }
  }, [legend])
}

/** One tooltip: what the token means, then whose move it is. */
function legendTip(entry: StatusLegendEntry | undefined): string | undefined {
  if (!entry) return undefined
  return `${entry.meaning}\nYour move: ${entry.operator_action}`
}

function formatClockTime(iso: string | null | undefined): string {
  if (!iso) return NOT_RECORDED
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleTimeString()
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
 * The fixed lifecycle, always all seven phases.
 *
 * A phase with no milestones is `SKIPPED`, not absent, so the row stays. Hiding
 * it would make two documents with different amounts of work look like two
 * different lifecycles, which is exactly what the fixed topology exists to deny.
 */
function PhaseStrip({ phases, legend }: { phases: PhaseView[]; legend: LegendLookup | null }) {
  return (
    <ol className="phaseStrip" aria-label="Lifecycle phases">
      {phases.map((phase) => (
        <li
          key={phase.phase}
          className={`phaseChip phase-${phase.status}`}
          title={legendTip(legend?.phase.get(phase.status))}
        >
          <span className="phaseName">{phase.phase}</span>
          <span className="phaseStatus">{phase.status}</span>
        </li>
      ))}
    </ol>
  )
}

/** The four vocabularies the cockpit renders, in reading order. */
const LEGEND_SECTIONS = [
  ['WorkUnit', 'work_unit'],
  ['Phase', 'phase'],
  ['Milestone', 'milestone'],
  ['Dispatch', 'dispatch'],
] as const

/**
 * The whole legend, one click away instead of one assistant question away.
 *
 * Collapsed by default because it is reference material, and placed under the
 * phase strip because that is where the bare tokens used to raise the question.
 */
function StatusLegendPanel({ legend }: { legend: LegendLookup | null }) {
  if (!legend) return null
  return (
    <details className="statusLegend">
      <summary>What these statuses mean</summary>
      <div className="legendSections">
        {LEGEND_SECTIONS.map(([label, key]) => (
          <section key={key} aria-label={`${label} statuses`}>
            <h3>{label}</h3>
            <table className="legendTable">
              <tbody>
                {legend.view[key].map((entry) => (
                  <tr key={entry.status}>
                    <td className="legendToken">
                      {entry.status}
                      {entry.terminal && <span className="legendTerminal">terminal</span>}
                    </td>
                    <td>
                      {entry.meaning}{' '}
                      <span className="legendMove">Your move: {entry.operator_action}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))}
      </div>
    </details>
  )
}

/**
 * RUNNING covers two situations an operator must tell apart: an agent actually
 * working, and a milestone parked because its dispatch intent has no claimant.
 * The intent's live status is the distinction, when the view carries one.
 */
function milestoneStatusLabel(milestone: MilestoneView): string {
  if (milestone.status !== 'RUNNING' || !milestone.dispatch_status) return milestone.status
  if (milestone.dispatch_status === 'PENDING') return 'WAITING ON DISPATCH'
  if (milestone.dispatch_status === 'CLAIMED' || milestone.dispatch_status === 'IN_PROGRESS')
    return 'AGENT RUNNING'
  return milestone.status
}

/** The synthesized RUNNING labels describe the dispatch, so its legend explains them. */
function milestoneStatusTip(
  milestone: MilestoneView,
  legend: LegendLookup | null,
): string | undefined {
  if (!legend) return undefined
  if (milestone.status === 'RUNNING' && milestone.dispatch_status) {
    const dispatch = legendTip(legend.dispatch.get(milestone.dispatch_status))
    if (dispatch) return dispatch
  }
  return legendTip(legend.milestone.get(milestone.status))
}

/**
 * Diagnostic evidence leads; the why-row exists for the failure reading. A type
 * missing from this list still renders - it just sorts with the ordinary
 * evidence - so drift against DiagnosticArtifactKind degrades to a wrong order
 * rather than hidden data.
 */
const DIAGNOSTIC_TYPES_FIRST = ['dispatch_failure_evidence', 'runner_crash_traceback']

function attemptArtifacts(milestone: MilestoneView, artifacts: ArtifactView[]): ArtifactView[] {
  const rank = (item: ArtifactView) => {
    const index = DIAGNOSTIC_TYPES_FIRST.indexOf(item.artifact_type)
    return index === -1 ? DIAGNOSTIC_TYPES_FIRST.length : index
  }
  return artifacts
    .filter((item) => item.milestone_execution_id === milestone.milestone_execution_id)
    .sort((left, right) => rank(left) - rank(right))
}

/**
 * One milestone row: what it is, how it went, and whether its evidence exists.
 *
 * A row that stopped on a failure grows a "why?" toggle. BLOCKED means a
 * correctable failure parked for the operator, and the operator cannot fix a
 * cause they have to run a database query to read - so the failure code, the
 * full summary, the dispatch intent, and the recorded failure evidence all
 * open inline under the row.
 */
function MilestoneRow({
  milestone,
  artifacts,
  legend,
}: {
  milestone: MilestoneView
  artifacts: ArtifactView[]
  legend: LegendLookup | null
}) {
  const [whyOpen, setWhyOpen] = useState(false)
  const missing = milestone.required_artifacts.filter(
    (artifact) => !milestone.produced_artifacts.includes(artifact),
  )
  const running = milestone.status === 'RUNNING'
  const staleOutcome =
    milestone.failure_code !== null || milestone.failure_summary !== null
  const hasWhy =
    milestone.status === 'BLOCKED' ||
    milestone.status === 'FAILED' ||
    milestone.failure_code !== null ||
    milestone.failure_summary !== null
  const own = hasWhy ? attemptArtifacts(milestone, artifacts) : []
  const entry = legend?.milestone.get(milestone.status)
  return (
    <Fragment>
      <tr className={`milestone-${milestone.status}`}>
        <td>
          <span className="milestoneKey">{milestone.stable_key}</span>
          <span className="milestoneTitle">{milestone.title}</span>
        </td>
        <td>{milestone.phase}</td>
        <td>
          <span title={milestoneStatusTip(milestone, legend)}>
            {milestoneStatusLabel(milestone)}
          </span>
          {milestone.attempt > 1 && <span className="pill">attempt {milestone.attempt}</span>}
        </td>
        <td>{milestone.dependencies.join(', ') || NOT_RECORDED}</td>
        <td>
          {/* Evidence is what gates completion, so the absent ones are the news.
              While an agent is working, absent evidence is the expected state
              rather than a finding, and colouring it as a failure read as a
              stall to the first operator who saw it. */}
          {milestone.produced_artifacts.join(', ') || NOT_RECORDED}
          {missing.length > 0 && (
            <span className={running ? 'laneMuted' : 'laneFailure'}>
              {running ? 'awaiting' : 'missing'} {missing.join(', ')}
            </span>
          )}
        </td>
        <td>
          {/* A running attempt has no outcome yet, so anything in these columns
              belongs to the attempt before it. Showing that as the current
              outcome made a live agent look stuck: attempt 3's elapsed dispatch
              sat in red beside "AGENT RUNNING · attempt 4". */}
          {running && staleOutcome && (
            <span className="laneMuted">previous attempt: </span>
          )}
          {milestone.failure_code && (
            <span className={running ? 'laneMuted' : 'laneFailure'}>{milestone.failure_code}</span>
          )}
          {milestone.failure_summary ?? milestone.result_summary ?? NOT_RECORDED}
          {hasWhy && (
            <button
              type="button"
              className="whyButton"
              aria-expanded={whyOpen}
              onClick={() => setWhyOpen((open) => !open)}
            >
              {whyOpen ? 'hide' : 'why?'}
            </button>
          )}
        </td>
      </tr>
      {whyOpen && (
        <tr className={`milestoneWhy milestone-${milestone.status}`}>
          <td colSpan={6}>
            {entry && (
              <p className="whyLegend">
                {entry.meaning} <strong>Your move:</strong> {entry.operator_action}
              </p>
            )}
            <dl className="executionFacts whyFacts">
              <Field label="Failure code" value={milestone.failure_code} />
              <Field
                label="What happened"
                value={milestone.failure_summary ?? milestone.result_summary}
              />
              <Field label="Dispatch intent" value={milestone.dispatch_intent_id} />
              <Field label="Dispatch status" value={milestone.dispatch_status} />
            </dl>
            {own.length > 0 ? (
              <ArtifactList artifacts={own} />
            ) : (
              <p className="cockpitEmpty">No evidence was recorded for this milestone.</p>
            )}
          </td>
        </tr>
      )}
    </Fragment>
  )
}

function MilestoneTable({
  workUnit,
  legend,
}: {
  workUnit: WorkUnitView
  legend: LegendLookup | null
}) {
  return (
    <section className="panel" aria-label="Milestones">
      <header className="panelHeader">
        <h2>Milestones</h2>
      </header>
      <div className="tableWrap">
        <table>
          <thead>
            <tr>
              <th>Milestone</th>
              <th>Phase</th>
              <th>Status</th>
              <th>Depends on</th>
              <th>Evidence</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {workUnit.milestones.map((milestone) => (
              <MilestoneRow
                key={milestone.stable_key}
                milestone={milestone}
                artifacts={workUnit.artifacts}
                legend={legend}
              />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function BlockingBanner({ blocking }: { blocking: BlockingCondition }) {
  if (blocking.kind === 'NONE') return null
  return (
    <p className={`blockingBanner blocking-${blocking.kind}`} role="status">
      <ShieldQuestion aria-hidden />
      <span>{blocking.detail}</span>
      {blocking.milestone_keys.length > 0 && (
        <span className="pill">{blocking.milestone_keys.join(', ')}</span>
      )}
    </p>
  )
}

/**
 * The approval gate, as two explicit buttons.
 *
 * Approving and denying are both decisions the compiled plan asked for, so
 * neither is a default and neither is hidden behind a menu. A pending request is
 * the only thing standing between this WorkUnit and its next phase.
 */
function PendingDecisions({
  workUnit,
  onSettled,
}: {
  workUnit: WorkUnitView
  onSettled: () => Promise<void>
}) {
  const { submit, pending, error } = useDecisionSubmission(workUnit.work_unit_id, onSettled)
  if (workUnit.pending_decisions.length === 0) return null
  return (
    <section className="panel" aria-label="Pending operator decisions">
      <header className="panelHeader">
        <h2>Waiting on you</h2>
      </header>
      {error && <p className="projectActionError">{error}</p>}
      <ul className="decisionList">
        {workUnit.pending_decisions.map((request) => (
          <li key={request.request_id}>
            <p className="decisionPrompt">{request.prompt}</p>
            <p className="decisionMeta">
              {request.request_kind} · requested {formatClockTime(request.created_at)}
            </p>
            <div className="decisionActions">
              <button
                type="button"
                disabled={pending}
                onClick={() => void submit(request.request_id, 'APPROVED')}
              >
                <CheckCircle2 aria-hidden /> Approve
              </button>
              <button
                type="button"
                className="deny"
                disabled={pending}
                onClick={() => void submit(request.request_id, 'DENIED')}
              >
                <XCircle aria-hidden /> Deny
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ArtifactList({ artifacts }: { artifacts: ArtifactView[] }) {
  if (artifacts.length === 0) {
    return <p className="cockpitEmpty">No evidence has been recorded yet.</p>
  }
  return (
    <ul className="artifactList">
      {artifacts.map((artifact) => (
        <li key={artifact.artifact_id}>
          <span className="timelineKind">{artifact.artifact_type}</span>
          <span className="artifactUri" title={artifact.uri}>
            {artifact.uri}
          </span>
          {/* The hash is what makes this evidence rather than a claim. */}
          <span className="artifactHash">{artifact.content_hash.slice(0, 12)}</span>
        </li>
      ))}
    </ul>
  )
}

function EventList({ events }: { events: EventView[] }) {
  if (events.length === 0) {
    return <p className="cockpitEmpty">No domain events yet.</p>
  }
  return (
    <ol className="timelineList">
      {[...events].reverse().map((event) => (
        <li key={event.event_id}>
          <span className="laneStamp">{event.sequence_number}</span>
          <span className="timelineKind">{event.event_type}</span>
          <span className="phaseName">{event.phase ?? NOT_RECORDED}</span>
          <span className="timelinePayload">
            {String(event.payload.milestone_key ?? '')}
          </span>
          <span className="laneStamp">{formatClockTime(event.occurred_at)}</span>
        </li>
      ))}
    </ol>
  )
}

/**
 * The numbered list of what a person does next, and what the work asks of them.
 *
 * Two questions an operator standing in front of a stopped WorkUnit has, which
 * the cockpit answered neither of. "What do I run?" is answered by the same
 * `next_commands` rule tables the terminal prints from, fetched rather than
 * re-derived here so the two surfaces cannot drift. "What am I supposed to
 * *do*?" is answered by the blocking milestone's own description and acceptance
 * criteria: a milestone titled "on-device operator verification" does not say
 * what to verify, and until now the design document was the only place that did.
 *
 * Numbered, because the ready commands are ordered and an operator asked for a
 * list they could work down. Refused and unproved commands keep their place
 * below the ready ones with the reason attached, matching the terminal's
 * grouping: an operator who does not see a verb ruled out here will reach for it
 * and spend a command learning what this already knows.
 */
function OperatorPlaybook({
  workUnit,
  nextCommands,
  error,
}: {
  workUnit: WorkUnitView
  nextCommands: NextCommandSet | null
  error: string | null
}) {
  const blockingKeys = new Set(workUnit.blocking?.milestone_keys ?? [])
  const waiting = workUnit.milestones.filter(
    (milestone) =>
      blockingKeys.has(milestone.stable_key) &&
      (milestone.description !== '' || milestone.acceptance_criteria.length > 0),
  )
  const ready = nextCommands?.commands.filter((item) => item.status === 'READY') ?? []
  const blocked = nextCommands?.commands.filter((item) => item.status !== 'READY') ?? []
  if (error === null && nextCommands === null && waiting.length === 0) return null

  return (
    <section className="panel" aria-label="What you need to do">
      <header className="panelHeader">
        <h2>What you need to do</h2>
      </header>
      {error && <p className="projectActionError">{error}</p>}
      {nextCommands && <p className="playbookHeadline">{nextCommands.headline}</p>}
      {nextCommands?.detail && <p className="playbookDetail">{nextCommands.detail}</p>}

      {waiting.map((milestone) => (
        <div key={milestone.stable_key} className="playbookAsk">
          <p className="playbookAskTitle">
            {milestone.stable_key} · {milestone.title} asks of you:
          </p>
          {milestone.description !== '' && (
            <p className="playbookAskBody">{milestone.description}</p>
          )}
          {milestone.acceptance_criteria.length > 0 && (
            <ul className="playbookCriteria">
              {milestone.acceptance_criteria.map((criterion) => (
                <li key={criterion}>{criterion}</li>
              ))}
            </ul>
          )}
        </div>
      ))}

      {ready.length > 0 && (
        <ol className="playbookList">
          {ready.map((item) => (
            <li key={item.command}>
              <p className="playbookIntent">{item.intent}</p>
              <code className="playbookCommand">{item.command}</code>
            </li>
          ))}
        </ol>
      )}

      {blocked.length > 0 && (
        <details className="playbookRuledOut">
          <summary>{blocked.length} command(s) ruled out in this state</summary>
          <ul>
            {blocked.map((item) => (
              <li key={item.command}>
                <p className="playbookIntent">{item.intent}</p>
                <code className="playbookCommand ruledOut">{item.command}</code>
                <p className="playbookReason">
                  {item.status}
                  {item.refusal_code ? ` · ${item.refusal_code}` : ''}
                  {item.reason ? ` - ${item.reason}` : ''}
                </p>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

// Settled means nothing here will ever need an operator again. FAILED is
// deliberately not in this set: a failed unit is the one that most needs a
// person, so it stays visible by default.
const SETTLED_WORK_UNIT_STATUSES = new Set(['SUCCEEDED', 'CANCELLED'])

function WorkUnitPicker({
  workUnits,
  selected,
  onSelect,
}: {
  workUnits: WorkUnitSummary[]
  selected: string
  onSelect: (workUnitId: string) => void
}) {
  const [showSettled, setShowSettled] = useState(false)
  const settledCount = workUnits.filter((unit) =>
    SETTLED_WORK_UNIT_STATUSES.has(unit.status),
  ).length
  // The selected unit is always listed, settled or not: a <select> whose value
  // has no matching <option> silently renders as unselected.
  const visible = workUnits.filter(
    (unit) =>
      showSettled ||
      unit.work_unit_id === selected ||
      !SETTLED_WORK_UNIT_STATUSES.has(unit.status),
  )
  return (
    <div className="workUnitPicker">
      <label>
        <span className="eyebrow">WorkUnit</span>
        <select value={selected} onChange={(event) => onSelect(event.target.value)}>
          <option value="">Select a WorkUnit</option>
          {visible.map((unit) => (
            <option key={unit.work_unit_id} value={unit.work_unit_id}>
              {unit.title} · {unit.status} · {unit.work_unit_id}
            </option>
          ))}
        </select>
      </label>
      {settledCount > 0 && (
        <label className="mx-auto">
          <input
            type="checkbox"
            checked={showSettled}
            onChange={(event) => setShowSettled(event.target.checked)}
          />
          <span className="whitespace-nowrap ml-0">
            Show {settledCount} settled (SUCCEEDED / CANCELLED)
          </span>
        </label>
      )}
    </div>
  )
}

export function WorkUnitCockpit({
  selectedWorkUnit,
  onSelectWorkUnit,
}: {
  selectedWorkUnit: string
  onSelectWorkUnit: (workUnitId: string) => void
}) {
  const list = useWorkUnitList()
  const detail = useWorkUnit(selectedWorkUnit)
  const events = useWorkUnitEvents(selectedWorkUnit)
  const operator = useWorkUnitNextCommands(selectedWorkUnit)
  const statusLegend = useStatusLegend()
  const legend = useLegendLookup(statusLegend.legend)

  const refreshAll = async () => {
    await Promise.all([detail.refresh(), events.refresh(), operator.refresh()])
  }

  return (
    <section className="workUnitCockpit" aria-label="WorkUnit cockpit">
      <header className="projectCockpitHeader">
        <div>
          <p className="eyebrow">Governed work</p>
          <h2>{detail.workUnit?.title ?? 'No WorkUnit selected'}</h2>
        </div>
        <WorkUnitPicker
          workUnits={list.workUnits}
          selected={selectedWorkUnit}
          onSelect={onSelectWorkUnit}
        />
        <button type="button" className="iconButton" onClick={() => void refreshAll()}>
          <RefreshCcw aria-hidden /> Refresh
        </button>
      </header>

      {list.error && <p className="projectActionError">{list.error}</p>}
      {detail.error && <p className="projectActionError">{detail.error}</p>}
      {statusLegend.error && (
        <p className="projectActionError">
          Status explanations are unavailable: {statusLegend.error}
        </p>
      )}

      {detail.workUnit === null ? (
        <p className="cockpitEmpty">
          Select a WorkUnit to see its lifecycle, milestones, and evidence.
        </p>
      ) : (
        <>
          <div className="workUnitLead">
            <span
              className={`actionPill status-${detail.workUnit.status}`}
              title={legendTip(legend?.workUnit.get(detail.workUnit.status))}
            >
              {detail.workUnit.status}
            </span>
            <span className="pill">{detail.workUnit.current_phase}</span>
            {detail.workUnit.failure_summary && (
              <span className="laneFailure">{detail.workUnit.failure_summary}</span>
            )}
          </div>

          <BlockingBanner blocking={detail.workUnit.blocking} />
          <OperatorPlaybook
            workUnit={detail.workUnit}
            nextCommands={operator.nextCommands}
            error={operator.error}
          />
          <PhaseStrip phases={detail.workUnit.phases} legend={legend} />
          <StatusLegendPanel legend={legend} />
          <PendingDecisions workUnit={detail.workUnit} onSettled={refreshAll} />
          <RuntimeControls onRan={refreshAll} />

          <div className="cockpitFactColumns">
            <section className="panel" aria-label="WorkUnit identity">
              <header className="panelHeader">
                <h2>Identity</h2>
              </header>
              <dl className="executionFacts">
                <Field label="WorkUnit" value={detail.workUnit.work_unit_id} />
                <Field label="Root workflow" value={detail.workUnit.root_workflow_id} />
                <Field label="DesignDoc revision" value={detail.workUnit.design_doc_revision_id} />
                <Field
                  label="Compiled plan"
                  value={detail.workUnit.compiled_plan_revision_id}
                />
                {/* The hash is the execution's authority; a mismatch fails closed. */}
                <Field
                  label="Plan hash"
                  value={detail.workUnit.compiled_plan_hash.slice(0, 16)}
                />
                <Field
                  label="Lifecycle"
                  value={`${detail.workUnit.lifecycle_profile} v${detail.workUnit.lifecycle_profile_version}`}
                />
                <Field label="Started" value={formatClockTime(detail.workUnit.started_at)} />
                <Field label="Completed" value={formatClockTime(detail.workUnit.completed_at)} />
              </dl>
            </section>

            <section className="panel" aria-label="Produced evidence">
              <header className="panelHeader">
                <h2>Evidence</h2>
              </header>
              <ArtifactList artifacts={detail.workUnit.artifacts} />
            </section>
          </div>

          <MilestoneTable workUnit={detail.workUnit} legend={legend} />

          <section className="panel" aria-label="Domain events">
            <header className="panelHeader">
              <h2>History</h2>
              {events.error && <span className="laneFailure">{events.error}</span>}
            </header>
            <EventList events={events.events} />
          </section>
        </>
      )}
    </section>
  )
}
