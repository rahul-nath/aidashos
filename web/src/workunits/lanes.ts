// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The WorkUnit cockpit's polling lanes.
 *
 * Same shape as the project cockpit's, and independent for the same reason: the
 * durable summary is re-read whole because any field can change, and the event
 * history is read forward from a sequence because it only ever grows. A stall in
 * one lane does not freeze the other, and neither lane participates in execution.
 * The cockpit reads projections; the root workflow does not wait on it.
 *
 * Every call goes through the generated client, so a renamed field or a route
 * that stops existing fails the build rather than rendering blanks.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { client, describeApiError } from '../api'
import type {
  EventView,
  NextCommandSet,
  StatusLegendView,
  WorkUnitSummary,
  WorkUnitView,
} from '../api'

export const WORK_UNIT_POLL_MS = 5_000
export const WORK_UNIT_EVENTS_POLL_MS = 3_000
export const WORK_UNIT_LIST_POLL_MS = 10_000
/** A page the server will serve and the cockpit can render in one pass. */
export const EVENT_PAGE_SIZE = 100
/** How much history stays on screen; the event log keeps the rest. */
export const RETAINED_EVENTS = 300

/**
 * Run `poll` now and then on an interval. Overlapping runs are suppressed so a
 * slow response cannot queue up behind itself.
 */
function usePolling(poll: () => Promise<void>, intervalMs: number) {
  useEffect(() => {
    let cancelled = false
    let running = false

    const tick = async () => {
      if (running || cancelled) return
      running = true
      try {
        await poll()
      } finally {
        running = false
      }
    }

    void tick()
    const id = window.setInterval(() => void tick(), intervalMs)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [poll, intervalMs])
}

export const STATUS_LEGEND_RETRY_MS = 30_000

export type StatusLegendLane = {
  legend: StatusLegendView | null
  error: string | null
}

/**
 * The status legend: a constant per server build, fetched until it arrives.
 *
 * The poll is a retry, not a refresh. Once the legend is held the lane stops
 * asking: the legend only changes on a server deploy, and this bundle already
 * bakes in the rest of the generated contract, so a mid-session upgrade needs a
 * reload either way.
 */
export function useStatusLegend(): StatusLegendLane {
  const [legend, setLegend] = useState<StatusLegendView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const held = useRef(false)

  const refresh = useCallback(async () => {
    if (held.current) return
    const { data, error: failed, response } = await client.GET('/status-legend', {})
    if (failed !== undefined || data === undefined) {
      setError(describeApiError(failed, response))
      return
    }
    held.current = true
    setLegend(data)
    setError(null)
  }, [])

  usePolling(refresh, STATUS_LEGEND_RETRY_MS)

  return { legend, error }
}

export type WorkUnitListLane = {
  workUnits: WorkUnitSummary[]
  error: string | null
  refresh: () => Promise<void>
}

/** Every WorkUnit, newest first, for choosing which one to look at. */
export function useWorkUnitList(): WorkUnitListLane {
  const [workUnits, setWorkUnits] = useState<WorkUnitSummary[]>([])
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    const { data, error: failed, response } = await client.GET('/work-units', {})
    if (failed !== undefined || data === undefined) {
      setError(describeApiError(failed, response))
      return
    }
    setWorkUnits(data.work_units)
    setError(null)
  }, [])

  usePolling(refresh, WORK_UNIT_LIST_POLL_MS)

  return { workUnits, error, refresh }
}

export type WorkUnitLane = {
  workUnit: WorkUnitView | null
  error: string | null
  refresh: () => Promise<void>
}

/** Lane one: the durable summary of one WorkUnit, re-read whole. */
export function useWorkUnit(workUnitId: string): WorkUnitLane {
  const [workUnit, setWorkUnit] = useState<WorkUnitView | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!workUnitId) return
    const { data, error: failed, response } = await client.GET('/work-units/{work_unit_id}', {
      params: { path: { work_unit_id: workUnitId } },
    })
    if (failed !== undefined || data === undefined) {
      setWorkUnit(null)
      setError(describeApiError(failed, response))
      return
    }
    setWorkUnit(data)
    setError(null)
  }, [workUnitId])

  usePolling(refresh, WORK_UNIT_POLL_MS)

  return { workUnit, error, refresh }
}

export type NextCommandLane = {
  nextCommands: NextCommandSet | null
  error: string | null
  refresh: () => Promise<void>
}

/**
 * Lane three: what the operator does next, computed server-side.
 *
 * Its own lane rather than a field on the WorkUnit, because it is derived from
 * that view rather than part of it, and because the rule tables that produce it
 * live in one module that the terminal already prints from. The cockpit asking
 * the same question of the same function is what keeps the two surfaces from
 * disagreeing about what unblocks a WorkUnit.
 */
export function useWorkUnitNextCommands(workUnitId: string): NextCommandLane {
  const [nextCommands, setNextCommands] = useState<NextCommandSet | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!workUnitId) return
    const { data, error: failed, response } = await client.GET(
      '/work-units/{work_unit_id}/next-commands',
      { params: { path: { work_unit_id: workUnitId } } },
    )
    if (failed !== undefined || data === undefined) {
      setNextCommands(null)
      setError(describeApiError(failed, response))
      return
    }
    setNextCommands(data)
    setError(null)
  }, [workUnitId])

  usePolling(refresh, WORK_UNIT_POLL_MS)

  return { nextCommands, error, refresh }
}

export type WorkUnitEventLane = {
  events: EventView[]
  error: string | null
  refresh: () => Promise<void>
}

/**
 * Lane two: the append-only history, read forward from the highest sequence held.
 *
 * WorkUnit sequence numbers are dense and monotonic within one WorkUnit, so the
 * cursor is just a number and there is no server cursor to reset. That is simpler
 * than the project timeline, whose sequences only mean something inside a lease.
 */
export function useWorkUnitEvents(workUnitId: string): WorkUnitEventLane {
  const [events, setEvents] = useState<EventView[]>([])
  const [error, setError] = useState<string | null>(null)
  // The cursor drives the next request, so it must not wait for a re-render.
  const afterSequence = useRef(0)

  const refresh = useCallback(async () => {
    if (!workUnitId) return
    const { data, error: failed, response } = await client.GET(
      '/work-units/{work_unit_id}/events',
      {
        params: {
          path: { work_unit_id: workUnitId },
          query: { after_sequence: afterSequence.current, limit: EVENT_PAGE_SIZE },
        },
      },
    )
    if (failed !== undefined || data === undefined) {
      setError(describeApiError(failed, response))
      return
    }
    setError(null)
    if (data.events.length === 0) return
    afterSequence.current = data.events[data.events.length - 1].sequence_number
    setEvents((current) => {
      // Sequence numbers are unique per WorkUnit, so a re-delivered page merges
      // rather than duplicating. The window keeps the newest.
      const merged = new Map(current.map((item) => [item.sequence_number, item]))
      for (const item of data.events) merged.set(item.sequence_number, item)
      return [...merged.values()]
        .sort((left, right) => left.sequence_number - right.sequence_number)
        .slice(-RETAINED_EVENTS)
    })
  }, [workUnitId])

  usePolling(refresh, WORK_UNIT_EVENTS_POLL_MS)

  return { events, error, refresh }
}

export type DecisionSubmission = {
  submit: (requestId: string, decision: 'APPROVED' | 'DENIED') => Promise<void>
  pending: boolean
  error: string | null
}

/**
 * Answer one named operator decision.
 *
 * The idempotency key is derived from the request and the decision rather than
 * generated fresh, so a double-clicked button and a retried request are the same
 * submission. The server would absorb a duplicate anyway; this keeps the client
 * from claiming two different ones.
 */
export function useDecisionSubmission(
  workUnitId: string,
  onSettled: () => Promise<void>,
): DecisionSubmission {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = useCallback(
    async (requestId: string, decision: 'APPROVED' | 'DENIED') => {
      setPending(true)
      try {
        const { error: failed, response } = await client.POST(
          '/work-units/{work_unit_id}/decisions',
          {
            params: { path: { work_unit_id: workUnitId } },
            body: {
              request_id: requestId,
              decision,
              idempotency_key: `${requestId}:${decision}`,
              decided_by: 'cockpit',
              payload: {},
            },
          },
        )
        if (failed !== undefined) {
          setError(describeApiError(failed, response))
          return
        }
        setError(null)
        await onSettled()
      } finally {
        setPending(false)
      }
    },
    [workUnitId, onSettled],
  )

  return { submit, pending, error }
}
