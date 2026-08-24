// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The typed HTTP client, generated rather than transcribed.
 *
 * `api-types.ts` is emitted from the application's own OpenAPI schema by
 * `make api-types`, so a renamed field or a new status member breaks the build
 * here instead of rendering as a blank cell in the cockpit. Do not hand-edit that
 * file, and do not re-declare its shapes: the aliases below are the only place a
 * server type gets a local name.
 *
 * `client` is preferred for every new call. `apiFetch` and `postJson` survive for
 * the routes whose responses the server still describes as bare objects, where a
 * typed call would buy nothing.
 */

import createClient from 'openapi-fetch'

import type { components, paths } from './api-types'

export const API_BASE =
  import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')

const API_REQUEST_TIMEOUT_MS = 30_000

/** Server contracts, each given a local name exactly once. */
export type WorkUnitView = components['schemas']['WorkUnitView']
export type WorkUnitSummary = components['schemas']['WorkUnitSummary']
export type MilestoneView = components['schemas']['MilestoneView']
export type PhaseView = components['schemas']['PhaseView']
export type ArtifactView = components['schemas']['ArtifactView']
export type EventView = components['schemas']['EventView']
export type PendingDecisionView = components['schemas']['PendingDecisionView']
export type BlockingCondition = components['schemas']['BlockingCondition']
export type NextCommandSet = components['schemas']['NextCommandSet']
export type NextCommand = components['schemas']['NextCommand']
export type WorkUnitStatus = components['schemas']['WorkUnitStatus']
export type MilestoneExecutionStatus = components['schemas']['MilestoneExecutionStatus']
export type PhaseStatus = components['schemas']['PhaseStatus']
export type DispatchIntentStatus = components['schemas']['DispatchIntentStatus']
export type StatusLegendView = components['schemas']['StatusLegendView']
export type StatusLegendEntry = components['schemas']['StatusLegendEntry']
export type LifecyclePhase = components['schemas']['LifecyclePhase']
export type OperatorDecision = components['schemas']['OperatorDecision']
export type WorkUnitDecisionResult = components['schemas']['WorkUnitDecisionResult']
export type WorkUnitResumeResult = components['schemas']['WorkUnitResumeResult']
export type WorkUnitCancelResult = components['schemas']['WorkUnitCancelResult']
export type ProjectActivityPage = components['schemas']['ProjectActivityPage']
export type ProjectCenterView = components['schemas']['ProjectCenterView']
export type LinkedProject = components['schemas']['ProjectStatusRow']
export type ProjectActionSnapshot = components['schemas']['ProjectActionSnapshot']
export type ActivityCursor = components['schemas']['ActivityCursor']
export type ExecutionEventEntry = components['schemas']['ExecutionEventEntry']
export type CheckpointFacts = components['schemas']['CheckpointFacts']
export type ApprovalFacts = components['schemas']['ApprovalFacts']
export type LeaseFacts = components['schemas']['LeaseFacts']
export type IntentFacts = components['schemas']['IntentFacts']
export type IntegrationTriggerResult =
  | components['schemas']['IntegrationAccepted']
  | components['schemas']['IntegrationRunning']
  | components['schemas']['IntegrationComplete']
  | components['schemas']['IntegrationBlocked']

/** One of two execution shapes, discriminated by `execution_kind` on the wire. */
export type ExecutionFacts = LeaseFacts | IntentFacts

export const client = createClient<paths>({
  baseUrl: API_BASE,
  fetch: (request: Request) =>
    fetch(request, { signal: AbortSignal.timeout(API_REQUEST_TIMEOUT_MS) }),
})

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${path}`, {
    ...init,
    signal: init?.signal ?? AbortSignal.timeout(API_REQUEST_TIMEOUT_MS),
  })
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await apiFetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw new Error(await response.text())
  }
  return response.json() as Promise<T>
}

/**
 * Render a failed typed call as a message.
 *
 * `openapi-fetch` returns errors rather than throwing, and FastAPI's failure body
 * is `{detail: ...}` where the detail is sometimes a string and sometimes an
 * object. Both shapes end up readable rather than as `[object Object]`.
 */
export function describeApiError(error: unknown, response?: Response): string {
  if (error && typeof error === 'object' && 'detail' in error) {
    const detail = (error as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    return JSON.stringify(detail)
  }
  if (typeof error === 'string') return error
  if (response) return `${response.status} ${response.statusText}`
  return 'request failed'
}
