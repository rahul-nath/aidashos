// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The buttons that start the loops a queued WorkUnit is waiting on.
 *
 * A WorkUnit reaches QUEUED and then needs two resident loops to move: the
 * drainer hands it to DBOS, and the dispatcher claims the intents its milestones
 * submit. Both already run from `start-agent-runtime.sh`, but when they are not
 * up the cockpit showed a WorkUnit sitting still with nothing to press, and the
 * only remedy was a terminal.
 *
 * These post to `/pi/directive`, which has always accepted the same directives
 * the terminal sends. Nothing new is being made possible here; a capability the
 * backend already had is being given a surface.
 */

import { Play, Radio } from 'lucide-react'
import { useCallback, useState } from 'react'

import { postJson } from '../api'

/** One directive, named as the operator thinks of it rather than as pi spells it. */
type RuntimeAction = {
  readonly id: string
  readonly label: string
  readonly directive: string
  readonly description: string
}

/**
 * Bounded by default.
 *
 * `--max-polls` matters: a directive fired from a browser has no terminal to
 * interrupt, so an unbounded loop would run until the server restarted with no
 * way to stop it from here. A bounded burst is the honest thing to offer until
 * there is a stop button to match a start one.
 */
const RUNTIME_ACTIONS: readonly RuntimeAction[] = [
  {
    id: 'dispatcher',
    label: 'Run dispatcher',
    directive: '/start /dispatcher --max-polls 5',
    description: 'Claim up to five pending dispatch intents and run them.',
  },
  // No drainer button yet, and deliberately not a broken one: there is no
  // drainer directive to send. `run_enqueue_drainer` exists on the CLI and MCP
  // only, so a button needs either a directive alias or the POST route the
  // design doc for the missing cockpit routes calls for.
]

export function RuntimeControls({ onRan }: { onRan: () => Promise<void> }) {
  const [running, setRunning] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<string | null>(null)

  const run = useCallback(
    async (action: RuntimeAction) => {
      setRunning(action.id)
      setError(null)
      setLastResult(null)
      try {
        await postJson<{ results?: unknown[] }>('/pi/directive', {
          text: action.directive,
          workspace_id: 'general',
        })
        setLastResult(`${action.label} finished`)
        await onRan()
      } catch (cause) {
        // The directive surface answers with prose, not a typed error, so the
        // message is shown as-is rather than mapped to a friendlier one that
        // would hide which directive failed.
        setError(cause instanceof Error ? cause.message : String(cause))
      } finally {
        setRunning(null)
      }
    },
    [onRan],
  )

  return (
    <section className="panel" aria-label="Runtime controls">
      <header className="panelHeader">
        <h2>
          <Radio aria-hidden /> Runtime
        </h2>
      </header>
      {error && <p className="projectActionError">{error}</p>}
      {lastResult && <p className="decisionMeta">{lastResult}</p>}
      <ul className="decisionList">
        {RUNTIME_ACTIONS.map((action) => (
          <li key={action.id}>
            <p className="decisionPrompt">{action.label}</p>
            <p className="decisionMeta">{action.description}</p>
            <div className="decisionActions">
              <button
                type="button"
                disabled={running !== null}
                onClick={() => void run(action)}
              >
                <Play aria-hidden /> {running === action.id ? 'Running…' : action.label}
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
