// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The WorkUnit cockpit shows durable lifecycle state and offers one action.
 *
 * These specs pin what an operator must be able to see and do without reading a
 * transcript or a log: all seven phases including the skipped ones, milestone
 * status with its evidence, what is blocking, and an approval that actually
 * resolves a named request.
 *
 * Fixtures are written against the generated schema, so a fixture that drifts from
 * the server is a build failure in the app rather than a passing test against a
 * shape the server cannot send.
 */

import { expect, test, type Page } from '@playwright/test'

const WORK_UNIT_ID = 'wu-acceptance'
const REQUEST_ID = 'wud_review_e'

function phase(name: string, status: string, milestoneKeys: string[] = []) {
  return { phase: name, status, milestone_keys: milestoneKeys }
}

function milestone(overrides: Record<string, unknown> = {}) {
  return {
    stable_key: 'a',
    title: 'plan the change',
    phase: 'PLAN',
    ordinal: 1,
    executor_kind: 'plan.implementation',
    status: 'SUCCEEDED',
    attempt: 1,
    requires_operator_approval: false,
    milestone_execution_id: 'mex_a',
    description: '',
    acceptance_criteria: [],
    dependencies: [],
    required_artifacts: ['implementation_plan'],
    produced_artifacts: ['implementation_plan'],
    child_workflow_id: null,
    dispatch_intent_id: null,
    dispatch_status: null,
    failure_code: null,
    failure_summary: null,
    result_summary: 'planned',
    ...overrides,
  }
}

function legendEntry(status: string, meaning: string, action: string, terminal = false) {
  return { status, meaning, operator_action: action, terminal }
}

/** A slice of `/status-legend`: the entries these specs assert on. */
const STATUS_LEGEND = {
  schema_version: 'status_legend.v1',
  work_unit: [
    legendEntry(
      'RUNNING',
      'The root workflow is executing milestones.',
      'No action; watch the milestones for BLOCKED or WAITING_FOR_OPERATOR.',
    ),
    legendEntry(
      'BLOCKED',
      'A correctable failure parked the work for you; nothing moves until you act.',
      'Fix the recorded cause, then resume the WorkUnit - or supersede it with a new one.',
    ),
    legendEntry(
      'SUCCEEDED',
      'Every phase completed and the required evidence was recorded.',
      'None; the work is done.',
      true,
    ),
  ],
  phase: [
    legendEntry(
      'PENDING',
      'The lifecycle has not reached this phase yet.',
      'No action; earlier phases run first.',
    ),
    legendEntry(
      'BLOCKED',
      'A milestone in this phase stopped on a correctable failure.',
      'Find the BLOCKED milestone in the table and clear it.',
    ),
  ],
  milestone: [
    legendEntry(
      'BLOCKED',
      'A correctable failure parked this milestone for you: the run stopped, and the ' +
        'cause and evidence are recorded on this row.',
      'Read the failure and evidence, fix the cause, then resume the WorkUnit - or supersede it.',
    ),
    legendEntry('SUCCEEDED', 'Completed with its required evidence recorded.', 'None.', true),
  ],
  dispatch: [
    legendEntry(
      'PENDING',
      'Queued for dispatch; no agent has claimed it yet.',
      'If it stays PENDING, nothing is draining the queue; start or check the dispatcher.',
    ),
    legendEntry(
      'FAILED',
      'The run failed; what the agent reported is kept on the intent and as failure evidence.',
      'Read the failure evidence, fix the cause, then resume the WorkUnit.',
      true,
    ),
  ],
}

/** The acceptance scenario mid-flight: review is parked on an operator decision. */
function workUnitView(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 'work_unit_view.v1',
    work_unit_id: WORK_UNIT_ID,
    title: 'Acceptance design doc',
    status: 'BLOCKED',
    current_phase: 'REVIEW',
    design_doc_revision_id: 'ddr_01de479f',
    compiled_plan_revision_id: 'cpr_a0ce5827',
    compiled_plan_hash: 'c2bb2720c7320c91fecc41baa77070428d9a330d2c04154a2a5a980184760c42',
    lifecycle_profile: 'engineering.v1',
    lifecycle_profile_version: 1,
    root_workflow_id: `work-unit:${WORK_UNIT_ID}`,
    supersedes_work_unit_id: null,
    legacy_saga_id: null,
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    completed_at: null,
    failure_code: null,
    failure_summary: null,
    phases: [
      phase('CLARIFY', 'SKIPPED'),
      phase('VALIDATE', 'SKIPPED'),
      phase('PLAN', 'SUCCEEDED', ['a']),
      phase('IMPLEMENT', 'SUCCEEDED', ['b', 'c']),
      phase('VERIFY', 'SUCCEEDED', ['d']),
      phase('REVIEW', 'BLOCKED', ['e']),
      phase('DELIVER', 'PENDING', ['f']),
    ],
    milestones: [
      milestone(),
      milestone({
        stable_key: 'b',
        title: 'implement the reader',
        phase: 'IMPLEMENT',
        milestone_execution_id: 'mex_b',
        dependencies: ['a'],
        required_artifacts: ['source_patch'],
        produced_artifacts: ['source_patch'],
      }),
      milestone({
        stable_key: 'e',
        title: 'staff review',
        phase: 'REVIEW',
        executor_kind: 'review.operator',
        status: 'BLOCKED',
        requires_operator_approval: true,
        milestone_execution_id: 'mex_e',
        description: 'Verify the built app on the physical device before approving it.',
        acceptance_criteria: [
          'the highlighted line matches audible narration and a seek re-resolves within one second',
        ],
        dependencies: ['d'],
        required_artifacts: ['operator_approval'],
        produced_artifacts: [],
        failure_code: 'operator_decision_pending',
        failure_summary: 'the approval wait elapsed with no operator decision',
        result_summary: null,
      }),
      milestone({
        stable_key: 'f',
        title: 'deliver the artifact',
        phase: 'DELIVER',
        executor_kind: 'deliver.artifact',
        status: 'PENDING',
        milestone_execution_id: 'mex_f',
        dependencies: ['e'],
        required_artifacts: ['delivery_record'],
        produced_artifacts: [],
        result_summary: null,
      }),
    ],
    blocking: {
      kind: 'OPERATOR_DECISION',
      detail: 'an operator decision is required before this work can continue',
      milestone_keys: ['e'],
    },
    pending_decisions: [
      {
        request_id: REQUEST_ID,
        request_kind: 'APPROVAL',
        prompt: 'Approve milestone e (staff review) before review.operator proceeds.',
        milestone_execution_id: 'mex_e',
        created_at: new Date().toISOString(),
      },
    ],
    artifacts: [
      {
        artifact_id: 'wua_plan',
        artifact_type: 'implementation_plan',
        uri: `workunit://${WORK_UNIT_ID}/a/implementation_plan`,
        content_hash: 'abc123def456789',
        milestone_execution_id: 'mex_a',
        producer_workflow_id: `work-unit:${WORK_UNIT_ID}`,
        producer_step_name: 'simulate:plan.implementation',
        created_at: new Date().toISOString(),
      },
    ],
    recent_events: [],
    dbos_workflow_ids: [`work-unit:${WORK_UNIT_ID}`],
    ...overrides,
  }
}

function event(sequence: number, eventType: string, extra: Record<string, unknown> = {}) {
  return {
    event_id: `wue_${sequence}`,
    sequence_number: sequence,
    event_type: eventType,
    phase: 'REVIEW',
    milestone_execution_id: 'mex_e',
    root_workflow_id: `work-unit:${WORK_UNIT_ID}`,
    child_workflow_id: null,
    occurred_at: new Date().toISOString(),
    payload: { milestone_key: 'e' },
    ...extra,
  }
}

async function stubShell(page: Page, statusLegendStatus = 200) {
  await page.route('**/dashboard', (route) =>
    route.fulfill({
      json: {
        workflow_count: 0,
        manual_review_queue_depth: 0,
        failed_workflow_count: 0,
        embedding_chunk_count: 0,
        egress_write_count: 0,
        deduped_egress_count: 0,
        recent_workflows: [],
      },
    }),
  )
  await page.route('**/workflows?**', (route) => route.fulfill({ json: { workflows: [] } }))
  await page.route('**/projects', (route) => route.fulfill({ json: { projects: [] } }))
  await page.route('**/action', (route) => route.fulfill({ status: 503, json: { detail: 'no project' } }))
  await page.route('**/activity?**', (route) =>
    route.fulfill({ status: 503, json: { detail: 'no project' } }),
  )
  await page.route('**/status-legend', (route) =>
    route.fulfill({
      status: statusLegendStatus,
      json: statusLegendStatus === 200 ? STATUS_LEGEND : { detail: 'legend unavailable' },
    }),
  )
}

async function stubWorkUnit(page: Page, view: Record<string, unknown>) {
  await page.route('**/work-units', (route) =>
    route.fulfill({
      json: {
        work_units: [
          {
            work_unit_id: WORK_UNIT_ID,
            title: 'Acceptance design doc',
            status: String(view.status),
            current_phase: String(view.current_phase),
            root_workflow_id: `work-unit:${WORK_UNIT_ID}`,
            compiled_plan_hash: String(view.compiled_plan_hash),
          },
        ],
      },
    }),
  )
  await page.route(`**/work-units/${WORK_UNIT_ID}`, (route) => route.fulfill({ json: view }))
  await page.route(`**/work-units/${WORK_UNIT_ID}/events?**`, (route) =>
    route.fulfill({
      json: {
        work_unit_id: WORK_UNIT_ID,
        events: [event(29, 'MILESTONE_BLOCKED'), event(30, 'PHASE_BLOCKED')],
      },
    }),
  )
  await page.route(`**/work-units/${WORK_UNIT_ID}/next-commands`, (route) =>
    route.fulfill({
      json: {
        schema_version: 'next_commands.v1',
        headline: 'BLOCKED  a correctable failure parked this work for you',
        detail: 'milestone e needs an operator decision',
        commands: [
          {
            command: `agent-ledger resume_work_unit ${WORK_UNIT_ID}`,
            intent: 're-drive the parked work',
            status: 'READY',
            precondition: 'the staffed harnesses can act',
            reason: null,
            refusal_code: null,
          },
          {
            command: `agent-ledger adopt_settled_work_unit_dispatch ${WORK_UNIT_ID} e`,
            intent: 'credit a settled dispatch',
            status: 'REFUSED',
            precondition: 'the milestone has a DONE dispatch',
            reason: 'the operator milestone has no dispatch intent',
            refusal_code: 'settled_adoption_dispatch_missing',
          },
        ],
      },
    }),
  )
}

async function selectWorkUnit(page: Page) {
  const cockpit = page.getByLabel('WorkUnit cockpit')
  await cockpit.getByRole('combobox').selectOption(WORK_UNIT_ID)
  return cockpit
}

test.describe('work unit cockpit', () => {
  test('shows all seven phases, including the ones with no work', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const strip = cockpit.getByLabel('Lifecycle phases')

    // A phase with no milestones is SKIPPED, not absent. Hiding it would make two
    // documents with different amounts of work look like different lifecycles.
    for (const name of [
      'CLARIFY',
      'VALIDATE',
      'PLAN',
      'IMPLEMENT',
      'VERIFY',
      'REVIEW',
      'DELIVER',
    ]) {
      await expect(strip.getByText(name, { exact: true })).toBeVisible()
    }
    await expect(strip.getByText('SKIPPED')).toHaveCount(2)
  })

  test('names the blocking condition and the milestone it belongs to', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)

    await expect(
      cockpit.getByText('an operator decision is required before this work can continue'),
    ).toBeVisible()
    await expect(cockpit.getByLabel('Pending operator decisions')).toContainText(
      'Approve milestone e (staff review)',
    )
  })

  test('shows a numbered operator playbook with the human acceptance check', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const playbook = cockpit.getByLabel('What you need to do')

    await expect(playbook).toContainText('Verify the built app on the physical device')
    await expect(playbook).toContainText(
      'the highlighted line matches audible narration and a seek re-resolves within one second',
    )
    await expect(playbook.locator('ol > li').first()).toContainText('re-drive the parked work')
    await expect(playbook.locator('ol > li').first()).toContainText(
      `agent-ledger resume_work_unit ${WORK_UNIT_ID}`,
    )
    await expect(playbook.getByText('1 command(s) ruled out in this state')).toBeVisible()
  })

  test('shows milestone evidence, and names what is missing', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const milestones = cockpit.getByLabel('Milestones')

    await expect(milestones.getByText('implementation_plan').first()).toBeVisible()
    // Required-but-absent evidence is the news, because evidence gates completion.
    await expect(milestones.getByText('missing operator_approval')).toBeVisible()
    await expect(milestones.getByText('missing delivery_record')).toBeVisible()
  })

  test('a running attempt labels old failure text as previous and awaits its artifact', async ({
    page,
  }) => {
    await stubShell(page)
    await stubWorkUnit(
      page,
      workUnitView({
        status: 'RUNNING',
        current_phase: 'IMPLEMENT',
        blocking: { kind: 'NONE', detail: 'nothing is blocking this work', milestone_keys: [] },
        pending_decisions: [],
        milestones: [
          milestone({
            stable_key: 'b',
            title: 'implement the reader',
            phase: 'IMPLEMENT',
            status: 'RUNNING',
            attempt: 4,
            milestone_execution_id: 'mex_b',
            required_artifacts: ['source_patch'],
            produced_artifacts: [],
            failure_code: 'USAGE_LIMIT',
            failure_summary: 'the previous provider quota was exhausted',
            result_summary: null,
          }),
        ],
      }),
    )
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const milestones = cockpit.getByLabel('Milestones')

    await expect(milestones.getByText('awaiting source_patch')).toBeVisible()
    await expect(milestones.getByText('previous attempt:')).toBeVisible()
    await expect(milestones.getByText('missing source_patch')).toHaveCount(0)
  })

  test('approving resolves the named request and the WorkUnit moves on', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    const submitted: Array<Record<string, unknown>> = []

    await page.route(`**/work-units/${WORK_UNIT_ID}/decisions`, async (route) => {
      submitted.push(route.request().postDataJSON())
      // After the decision, the server reports the resumed WorkUnit.
      await stubWorkUnit(
        page,
        workUnitView({
          status: 'SUCCEEDED',
          current_phase: 'COMPLETE',
          blocking: { kind: 'NONE', detail: 'nothing is blocking this work', milestone_keys: [] },
          pending_decisions: [],
        }),
      )
      await route.fulfill({
        json: {
          work_unit_id: WORK_UNIT_ID,
          request_id: REQUEST_ID,
          decision: 'APPROVED',
          applied: true,
          milestone_key: 'e',
          sequence_number: 32,
          reason: null,
        },
      })
    })
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    await cockpit.getByRole('button', { name: 'Approve' }).click()

    // The decision names the request it resolves and carries an idempotency key,
    // because a decision that names nothing cannot unblock anything.
    await expect.poll(() => submitted.length).toBe(1)
    expect(submitted[0].request_id).toBe(REQUEST_ID)
    expect(submitted[0].decision).toBe('APPROVED')
    expect(submitted[0].idempotency_key).toBe(`${REQUEST_ID}:APPROVED`)
    await expect(cockpit.getByText('SUCCEEDED').first()).toBeVisible()
    await expect(cockpit.getByLabel('Pending operator decisions')).toHaveCount(0)
  })

  test('denying is offered as its own explicit decision', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    const submitted: Array<Record<string, unknown>> = []
    await page.route(`**/work-units/${WORK_UNIT_ID}/decisions`, (route) => {
      submitted.push(route.request().postDataJSON())
      return route.fulfill({
        json: {
          work_unit_id: WORK_UNIT_ID,
          request_id: REQUEST_ID,
          decision: 'DENIED',
          applied: true,
          milestone_key: 'e',
          sequence_number: 32,
          reason: null,
        },
      })
    })
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    await cockpit.getByRole('button', { name: 'Deny' }).click()

    await expect.poll(() => submitted.length).toBe(1)
    expect(submitted[0].decision).toBe('DENIED')
  })

  test('a rejected decision surfaces the server reason instead of failing silently', async ({
    page,
  }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.route(`**/work-units/${WORK_UNIT_ID}/decisions`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "decision request 'wud_review_e' is already RESOLVED" },
      }),
    )
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    await cockpit.getByRole('button', { name: 'Approve' }).click()

    await expect(cockpit.getByText(/already RESOLVED/)).toBeVisible()
  })

  test('the history lane reads the append-only event log', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const history = cockpit.getByLabel('Domain events')

    await expect(history.getByText('MILESTONE_BLOCKED')).toBeVisible()
    await expect(history.getByText('PHASE_BLOCKED')).toBeVisible()
  })

  test('a status explains itself instead of standing as a bare token', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)

    // Hovering the lead pill answers the question the operator used to ask an
    // assistant: what does this mean, and whose move is it?
    await expect(cockpit.locator('.actionPill')).toHaveAttribute(
      'title',
      /parked the work for you[\s\S]*Your move:/,
    )
    // And the expandable legend carries the whole vocabulary, meaning plus move.
    await cockpit.getByText('What these statuses mean').click()
    await expect(
      cockpit.getByText('A correctable failure parked this milestone for you', { exact: false }),
    ).toBeVisible()
    await expect(cockpit.getByText('nothing is draining the queue', { exact: false })).toBeVisible()
  })

  test('a missing status legend is visible instead of silently restoring bare tokens', async ({
    page,
  }) => {
    await stubShell(page, 503)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)

    await expect(cockpit.getByText('Status explanations are unavailable:', { exact: false }))
      .toBeVisible()
  })

  test('a blocked milestone opens its cause and failure evidence in place', async ({ page }) => {
    await stubShell(page)
    const failureSummary = 'the review agent failed before reporting a decision'
    await stubWorkUnit(
      page,
      workUnitView({
        milestones: [
          milestone(),
          milestone({
            stable_key: 'e',
            title: 'staff review',
            phase: 'REVIEW',
            status: 'BLOCKED',
            milestone_execution_id: 'mex_e',
            dependencies: ['d'],
            required_artifacts: ['review_decision'],
            produced_artifacts: [],
            dispatch_intent_id: 'di_e',
            dispatch_status: 'FAILED',
            failure_code: 'DISPATCH_FAILED',
            failure_summary: failureSummary,
            result_summary: null,
          }),
        ],
        artifacts: [
          {
            artifact_id: 'wua_failure',
            artifact_type: 'dispatch_failure_evidence',
            uri: `workunit://${WORK_UNIT_ID}/e/dispatch_failure_evidence`,
            content_hash: 'deadbeefcafe4321',
            milestone_execution_id: 'mex_e',
            producer_workflow_id: `work-unit:${WORK_UNIT_ID}`,
            producer_step_name: 'dispatch:di_e',
            created_at: new Date().toISOString(),
          },
        ],
      }),
    )
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const milestones = cockpit.getByLabel('Milestones')

    // The why is one click away, not a database query: the full summary, the
    // intent that failed, and the evidence the failed dispatch already wrote.
    await milestones.getByRole('button', { name: 'why?' }).click()
    await expect(milestones.getByText(failureSummary).last()).toBeVisible()
    await expect(milestones.getByText('di_e')).toBeVisible()
    await expect(milestones.getByText('dispatch_failure_evidence', { exact: true })).toBeVisible()
    await expect(
      milestones.getByText(`workunit://${WORK_UNIT_ID}/e/dispatch_failure_evidence`),
    ).toBeVisible()
  })

  test('the identity panel shows the plan the execution is bound to', async ({ page }) => {
    await stubShell(page)
    await stubWorkUnit(page, workUnitView())
    await page.goto('/')

    const cockpit = await selectWorkUnit(page)
    const identity = cockpit.getByLabel('WorkUnit identity')

    await expect(identity.getByText(`work-unit:${WORK_UNIT_ID}`)).toBeVisible()
    await expect(identity.getByText('ddr_01de479f')).toBeVisible()
    await expect(identity.getByText('cpr_a0ce5827')).toBeVisible()
    // The hash is the execution's authority, so it is on screen rather than implied.
    await expect(identity.getByText('c2bb2720c7320c91')).toBeVisible()
  })
})
