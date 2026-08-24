// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

import { expect, test } from '@playwright/test'
import type { Page, Route } from '@playwright/test'

/**
 * The cockpit reads current state and lifecycle history on two independent
 * lanes. These tests drive the browser against stubbed contract responses so
 * the lane behaviour itself is observable: history accumulates across polls
 * instead of restarting, a lease change clears history that belongs to the
 * previous execution, and neither lane stops when the other fails.
 */

const PROJECT_ID = 'pest_site_factory'

type ActivityPage = {
  schema_version: 'project_activity_page.v1'
  generated_at: string
  project_id: string
  lease_id: string | null
  cursor_reset: boolean
  events: {
    event_id: string
    sequence: number
    occurred_at: string
    source: string
    kind: string
    payload: Record<string, unknown>
  }[]
  has_more: boolean
  next_cursor: { lease_id: string; after_sequence: number } | null
}

function actionSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 'project_action_snapshot.v1',
    generated_at: new Date().toISOString(),
    freshness_seconds: 0,
    action: 'RECOVERABLE_FAILURE',
    summary: 'Execution stopped with a durable checkpoint available for recovery.',
    next_command: 'pi /ledger list_execution_checkpoints',
    runtime: { status: 'ok' },
    project: { id: PROJECT_ID, path: '/tmp/p', status: 'ok', branch: 'main', head_sha: 'abc12345' },
    saga: { saga_id: 'saga-1', status: 'EXECUTING' },
    milestone: { milestone_id: 'saga-1:m06', name: 'Hosted preview', status: 'FAILED' },
    execution: {
      // The server names which of the two execution shapes this is; a fixture
      // that omitted it would be describing a response the server cannot send.
      execution_kind: 'lease',
      lease_id: 'lease-current',
      status: 'CANCELED',
      outcome: 'SUPERVISOR_FAILED',
      activity_status: 'TERMINATED',
      agent_status: 'FAILED',
      agent_failure: 'agent exited before verification',
      supervisor_status: 'FAILED',
      supervisor_failure: 'event persistence failed: deadlock detected',
      persistence_status: 'FAILED',
      persistence_failure: 'could not append execution event',
      progress_assessment_status: 'NOT_REQUESTED',
      agent_tier: 'staff',
      agent_name: 'codex',
    },
    checkpoint: { checkpoint_id: 'ckpt-1', status: 'FAILED', reason: 'supervisor_failure' },
    approval: { approval_id: 'ap-1', request_type: 'CODE_MERGE', status: 'APPROVED' },
    warnings: [],
    source_ids: {},
    ...overrides,
  }
}

function event(sequence: number, kind: string) {
  return {
    event_id: `event-${kind}-${sequence}`,
    sequence,
    occurred_at: new Date().toISOString(),
    source: 'supervisor',
    kind,
    payload: { detail: `${kind} ${sequence}` },
  }
}

function activityPage(overrides: Partial<ActivityPage>): ActivityPage {
  return {
    schema_version: 'project_activity_page.v1',
    generated_at: new Date().toISOString(),
    project_id: PROJECT_ID,
    lease_id: 'lease-current',
    cursor_reset: false,
    events: [],
    has_more: false,
    next_cursor: null,
    ...overrides,
  }
}

async function stubShell(page: Page) {
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
  await page.route('**/projects', (route) =>
    route.fulfill({
      json: { projects: [{ id: PROJECT_ID, path: '/tmp/p', status: 'ok' }] },
    }),
  )
}

test.describe('project cockpit lanes', () => {
  test('an approved merge exposes one integration action and reports its durable handoff', async ({
    page,
  }) => {
    await stubShell(page)
    await page.route('**/action', (route) =>
      route.fulfill({
        json: actionSnapshot({
          action: 'MERGE_INTEGRATION_REQUIRED',
          summary: 'The exact approved commit is queued for integration.',
        }),
      }),
    )
    await page.route('**/activity?**', (route) => route.fulfill({ json: activityPage({}) }))
    let triggers = 0
    await page.route('**/approvals/ap-1/integration', (route) => {
      const isTrigger = route.request().method() === 'POST'
      if (isTrigger) triggers += 1
      route.fulfill({
        json: {
          state: isTrigger ? 'accepted' : 'complete',
          approval_id: 'ap-1',
          request_id: 'request-1',
          target_project_id: PROJECT_ID,
          message: isTrigger
            ? 'The approved request was handed to the refinery.'
            : 'The approved request is already integrated.',
        },
      })
    })

    await page.goto('/')

    const button = page.getByRole('button', { name: 'Integrate approved work' })
    await expect(button).toBeVisible()
    await button.click()
    await expect(page.getByRole('status')).toContainText('handed to the refinery')
    await expect(page.getByRole('status')).toContainText('already integrated', { timeout: 10_000 })
    expect(triggers).toBe(1)
  })

  test('no integration action is shown before CODE_MERGE approval', async ({ page }) => {
    await stubShell(page)
    await page.route('**/action', (route) => route.fulfill({ json: actionSnapshot() }))
    await page.route('**/activity?**', (route) => route.fulfill({ json: activityPage({}) }))

    await page.goto('/')

    await expect(page.getByRole('button', { name: 'Integrate approved work' })).toHaveCount(0)
  })

  test('surfaces every execution lane with its failure reason', async ({ page }) => {
    await stubShell(page)
    await page.route('**/action', (route) => route.fulfill({ json: actionSnapshot() }))
    await page.route('**/activity?**', (route) => route.fulfill({ json: activityPage({}) }))

    await page.goto('/')

    const cockpit = page.getByLabel('Project action cockpit')
    await expect(cockpit.getByText('RECOVERABLE_FAILURE')).toBeVisible()
    for (const reason of [
      'agent exited before verification',
      'event persistence failed: deadlock detected',
      'could not append execution event',
    ]) {
      await expect(cockpit.getByText(reason)).toBeVisible()
    }
    await expect(page.getByTestId('current-state-stamp')).toContainText('Current state as of')
  })

  test('history accumulates across polls instead of restarting', async ({ page }) => {
    await stubShell(page)
    await page.route('**/action', (route) => route.fulfill({ json: actionSnapshot() }))

    let poll = 0
    await page.route('**/activity?**', (route: Route) => {
      const url = new URL(route.request().url())
      const after = Number(url.searchParams.get('after_sequence') ?? 0)
      poll += 1
      // Only ever hand back what the caller has not already seen.
      const produced = poll === 1 ? [event(1, 'agent_started')] : [event(after + 1, 'agent_stdout')]
      const last = produced[produced.length - 1].sequence
      route.fulfill({
        json: activityPage({
          events: produced,
          next_cursor: { lease_id: 'lease-current', after_sequence: last },
        }),
      })
    })

    await page.goto('/')

    const rows = page.getByTestId('timeline-list').locator('li')
    await expect(rows).toHaveCount(1)
    await expect(rows.first()).toContainText('agent_started')
    // The second poll must extend the list, which only happens if the cockpit
    // sent the cursor it was given rather than re-reading from the start.
    await expect(rows).toHaveCount(2, { timeout: 15_000 })
    await expect(rows.nth(1)).toContainText('#2')
  })

  test('a page that repeats itself neither duplicates rows nor loops', async ({ page }) => {
    await stubShell(page)
    await page.route('**/action', (route) => route.fulfill({ json: actionSnapshot() }))

    let reads = 0
    // A server that keeps claiming more without moving the cursor.
    await page.route('**/activity?**', (route) => {
      reads += 1
      route.fulfill({
        json: activityPage({
          events: [event(1, 'lease_opened'), event(2, 'agent_started')],
          has_more: true,
          next_cursor: { lease_id: 'lease-current', after_sequence: 2 },
        }),
      })
    })

    await page.goto('/')

    const rows = page.getByTestId('timeline-list').locator('li')
    await expect(rows).toHaveCount(2)
    await page.waitForTimeout(4_000)
    await expect(rows).toHaveCount(2)
    // Two reads per poll at most: one that advances, one that proves it stopped.
    expect(reads).toBeLessThanOrEqual(6)
  })

  test('a new lease clears history that belongs to the previous execution', async ({ page }) => {
    await stubShell(page)
    await page.route('**/action', (route) => route.fulfill({ json: actionSnapshot() }))

    let poll = 0
    await page.route('**/activity?**', (route) => {
      poll += 1
      if (poll <= 1) {
        route.fulfill({
          json: activityPage({
            events: [event(1, 'agent_started'), event(2, 'agent_stdout')],
            next_cursor: { lease_id: 'lease-current', after_sequence: 2 },
          }),
        })
        return
      }
      route.fulfill({
        json: activityPage({
          lease_id: 'lease-retry',
          cursor_reset: true,
          events: [event(1, 'lease_opened')],
          next_cursor: { lease_id: 'lease-retry', after_sequence: 1 },
        }),
      })
    })

    await page.goto('/')

    const rows = page.getByTestId('timeline-list').locator('li')
    await expect(rows).toHaveCount(2)
    await expect(rows).toHaveCount(1, { timeout: 15_000 })
    await expect(rows.first()).toContainText('lease_opened')
    await expect(page.getByTestId('timeline-stamp')).toContainText('lease lease-re')
  })

  test('switching projects starts both lanes over on the new project', async ({ page }) => {
    const OTHER_PROJECT = 'ai_stack_local'
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
    await page.route('**/projects', (route) =>
      route.fulfill({
        json: {
          projects: [
            { id: PROJECT_ID, path: '/tmp/a', status: 'ok' },
            { id: OTHER_PROJECT, path: '/tmp/b', status: 'ok' },
          ],
        },
      }),
    )
    await page.route('**/action', (route) => route.fulfill({ json: actionSnapshot() }))
    await page.route('**/activity?**', (route) => {
      const url = new URL(route.request().url())
      const other = url.pathname.includes(OTHER_PROJECT)
      route.fulfill({
        json: activityPage({
          project_id: other ? OTHER_PROJECT : PROJECT_ID,
          lease_id: other ? 'lease-other' : 'lease-current',
          events: other ? [event(1, 'other_project_started')] : [event(1, 'agent_started')],
          next_cursor: {
            lease_id: other ? 'lease-other' : 'lease-current',
            after_sequence: 1,
          },
        }),
      })
    })

    await page.goto('/')

    const rows = page.getByTestId('timeline-list').locator('li')
    await expect(rows.first()).toContainText('agent_started')

    await page.getByLabel('Project action cockpit').getByRole('combobox').selectOption(OTHER_PROJECT)

    // The previous project's history must not survive the switch.
    await expect(rows).toHaveCount(1)
    await expect(rows.first()).toContainText('other_project_started')
    await expect(page.getByTestId('timeline-stamp')).toContainText('lease lease-ot')
  })

  test('the timeline keeps updating when the state lane is failing', async ({ page }) => {
    await stubShell(page)
    await page.route('**/action', (route) =>
      route.fulfill({ status: 503, body: 'coordination ledger unavailable' }),
    )
    await page.route('**/activity?**', (route) => {
      const after = Number(new URL(route.request().url()).searchParams.get('after_sequence') ?? 0)
      route.fulfill({
        json: activityPage({
          events: [event(after + 1, 'agent_stdout')],
          next_cursor: { lease_id: 'lease-current', after_sequence: after + 1 },
        }),
      })
    })

    await page.goto('/')

    await expect(page.getByText('coordination ledger unavailable')).toBeVisible()
    const rows = page.getByTestId('timeline-list').locator('li')
    await expect(rows).toHaveCount(1)
    await expect(rows).toHaveCount(2, { timeout: 15_000 })
  })
})
