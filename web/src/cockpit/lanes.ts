// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The cockpit's two polling lanes.
 *
 * They are deliberately independent. Current state is re-read whole on its own
 * interval, because any field can change without warning. Lifecycle history is
 * read forward from a cursor on its own interval, because it only ever grows.
 * Neither lane waits on the other, and a stall in one does not freeze the other.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, client, describeApiError } from '../api'
import type {
  ActivityCursor,
  ExecutionEventEntry,
  ProjectActionSnapshot,
  ProjectActivityPage,
} from '../api'

export const CURRENT_STATE_POLL_MS = 5_000
export const TIMELINE_POLL_MS = 3_000
/** A page the server will serve and the cockpit can render in one pass. */
export const TIMELINE_PAGE_SIZE = 50
/** How much history stays on screen; the ledger keeps the rest. */
export const TIMELINE_RETAINED_EVENTS = 200
/** Enough to catch up after a reconnect, still bounded per poll. */
const TIMELINE_MAX_PAGES_PER_POLL = TIMELINE_RETAINED_EVENTS / TIMELINE_PAGE_SIZE

type Lane<T> = {
  data: T
  error: string | null
  lastUpdatedAt: string | null
}

/**
 * Run `poll` now and then on an interval, until the component unmounts or the
 * inputs change. Overlapping runs are suppressed so a slow response cannot
 * queue up behind itself.
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

function describeError(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught)
}

/**
 * A lease's timeline is its events by sequence, not the order pages arrived.
 * Appending blindly would show a row twice if two pages ever overlapped.
 */
function mergeBySequence(
  current: ExecutionEventEntry[],
  incoming: ExecutionEventEntry[],
): ExecutionEventEntry[] {
  if (incoming.length === 0) return current
  const bySequence = new Map(current.map((event) => [event.sequence, event]))
  for (const event of incoming) {
    bySequence.set(event.sequence, event)
  }
  return [...bySequence.values()]
    .sort((left, right) => left.sequence - right.sequence)
    .slice(-TIMELINE_RETAINED_EVENTS)
}

export type CurrentStateLane = Lane<ProjectActionSnapshot | null> & {
  refresh: () => Promise<void>
}

/** Lane one: the authoritative answer to what the operator should do next. */
export function useCurrentState(projectId: string): CurrentStateLane {
  const [snapshot, setSnapshot] = useState<ProjectActionSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!projectId) return
    try {
      const response = await apiFetch(`/projects/${encodeURIComponent(projectId)}/action`)
      if (!response.ok) {
        setSnapshot(null)
        setError(await response.text())
        return
      }
      setSnapshot((await response.json()) as ProjectActionSnapshot)
      setError(null)
    } catch (caught) {
      setSnapshot(null)
      setError(describeError(caught))
    }
  }, [projectId])

  usePolling(refresh, CURRENT_STATE_POLL_MS)

  return { data: snapshot, error, lastUpdatedAt: snapshot?.generated_at ?? null, refresh }
}

export type TimelineLane = Lane<ExecutionEventEntry[]> & {
  leaseId: string | null
  hasMore: boolean
  refresh: () => Promise<void>
}

/** Lane two: the current lease's lifecycle events, read forward from a cursor. */
export function useLifecycleTimeline(projectId: string): TimelineLane {
  const [events, setEvents] = useState<ExecutionEventEntry[]>([])
  const [leaseId, setLeaseId] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null)
  // The cursor drives the next request, so it must not wait for a re-render.
  const cursor = useRef<ActivityCursor | null>(null)

  const readPage = useCallback(async (): Promise<ProjectActivityPage | null> => {
    // Path, params, and response shape are all checked against the server's own
    // schema, so a renamed query parameter fails the build rather than silently
    // restarting the timeline from sequence zero.
    const { data, error, response } = await client.GET('/projects/{project_id}/activity', {
      params: {
        path: { project_id: projectId },
        query: {
          limit: TIMELINE_PAGE_SIZE,
          ...(cursor.current
            ? {
                lease_id: cursor.current.lease_id,
                after_sequence: cursor.current.after_sequence,
              }
            : {}),
        },
      },
    })
    if (error !== undefined || data === undefined) {
      throw new Error(describeApiError(error, response))
    }
    return data
  }, [projectId])

  const refresh = useCallback(async () => {
    if (!projectId) return
    try {
      // Catching up after a gap should not take one interval per page, but a
      // poll still stops well before it could pull the whole transcript.
      let position = -1
      for (let page = 0; page < TIMELINE_MAX_PAGES_PER_POLL; page += 1) {
        const activity = await readPage()
        if (activity === null) return
        cursor.current = activity.next_cursor
        setLeaseId(activity.lease_id)
        setHasMore(activity.has_more)
        setLastUpdatedAt(activity.generated_at)
        setError(null)
        // A reset means the position we held belonged to a lease that is no
        // longer running, so what is on screen is the wrong execution.
        const restart = activity.cursor_reset || activity.lease_id === null
        setEvents((current) => mergeBySequence(restart ? [] : current, activity.events))
        // Keep reading only while the cursor is actually moving; a server that
        // reports more without advancing would otherwise be read in a loop.
        const advanced = activity.next_cursor?.after_sequence ?? -1
        if (!activity.has_more || advanced <= position) return
        position = advanced
      }
    } catch (caught) {
      setError(describeError(caught))
    }
  }, [projectId, readPage])

  usePolling(refresh, TIMELINE_POLL_MS)

  return { data: events, leaseId, hasMore, error, lastUpdatedAt, refresh }
}
