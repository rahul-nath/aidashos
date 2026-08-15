// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

import {
  Activity,
  AlertTriangle,
  Brain,
  Database,
  FileInput,
  Play,
  RefreshCcw,
  Search,
  ShieldCheck,
} from 'lucide-react'
import type { FormEvent, ReactNode } from 'react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import './App.css'
import TimerView from './TimerView'
import { apiFetch, postJson } from './api'
import type { LinkedProject } from './api'
import { ProjectCockpit } from './cockpit/ProjectCockpit'
import { WorkUnitCockpit } from './workunits/WorkUnitCockpit'

type DashboardSummary = {
  workflow_count: number
  manual_review_queue_depth: number
  failed_workflow_count: number
  embedding_chunk_count: number
  egress_write_count: number
  deduped_egress_count: number
  recent_workflows: WorkflowRow[]
}

type WorkflowRow = {
  workflow_id: string
  workflow_type: string
  workspace_id: string
  status: string
  current_stage: string
  updated_at: string
  last_error?: string | null
}

type SearchHit = {
  chunk_id: string
  artifact_id: string
  workspace_id: string
  text: string
  score: number
}

const defaultDashboard: DashboardSummary = {
  workflow_count: 0,
  manual_review_queue_depth: 0,
  failed_workflow_count: 0,
  embedding_chunk_count: 0,
  egress_write_count: 0,
  deduped_egress_count: 0,
  recent_workflows: [],
}

function App() {
  if (new URLSearchParams(window.location.search).get('view') === 'timer') {
    return <TimerView />
  }

  return <DashboardApp />
}

function DashboardApp() {
  const [dashboard, setDashboard] = useState<DashboardSummary>(defaultDashboard)
  const [workflows, setWorkflows] = useState<WorkflowRow[]>([])
  const [question, setQuestion] = useState('What owns workflow truth?')
  const [filePath, setFilePath] = useState('')
  const [retrievalQuery, setRetrievalQuery] = useState('DBOS Workflowy write boundary')
  const [searchHits, setSearchHits] = useState<SearchHit[]>([])
  const [workflowyParent, setWorkflowyParent] = useState('')
  const [workflowyContent, setWorkflowyContent] = useState('')
  const [output, setOutput] = useState('Ready.')
  const [busy, setBusy] = useState(false)
  const [projects, setProjects] = useState<LinkedProject[]>([])
  const [selectedWorkUnit, setSelectedWorkUnit] = useState('')
  const [selectedProject, setSelectedProject] = useState('pest_site_factory')

  const statusTone = useMemo(() => {
    if (dashboard.failed_workflow_count > 0) return 'danger'
    if (dashboard.manual_review_queue_depth > 0) return 'warn'
    return 'ok'
  }, [dashboard.failed_workflow_count, dashboard.manual_review_queue_depth])

  const refresh = useCallback(async () => {
    const [dashResponse, workflowResponse, projectsResponse] = await Promise.all([
      apiFetch('/dashboard'),
      apiFetch('/workflows?limit=20'),
      apiFetch('/projects'),
    ])
    if (dashResponse.ok) {
      setDashboard(await dashResponse.json())
    }
    if (workflowResponse.ok) {
      const payload = await workflowResponse.json()
      setWorkflows(payload.workflows ?? [])
    }
    if (projectsResponse.ok) {
      const payload = await projectsResponse.json()
      const linkedProjects = (payload.projects ?? []) as LinkedProject[]
      setProjects(linkedProjects)
      if (linkedProjects.length && !linkedProjects.some((item) => item.id === selectedProject)) {
        setSelectedProject(linkedProjects[0].id)
      }
    }
  }, [selectedProject])

  useEffect(() => {
    const id = window.setTimeout(() => {
      refresh().catch((error) => setOutput(String(error)))
    }, 0)
    return () => window.clearTimeout(id)
  }, [refresh])

  async function runAction(action: () => Promise<unknown>) {
    setBusy(true)
    try {
      const result = await action()
      setOutput(JSON.stringify(result, null, 2))
      await refresh()
    } catch (error) {
      setOutput(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  function submitQuestion(event: FormEvent) {
    event.preventDefault()
    runAction(() => postJson('/questions', { prompt: question, workspace_id: 'general' }))
  }

  function submitFile(event: FormEvent) {
    event.preventDefault()
    runAction(() =>
      postJson('/ingress/file', {
        path: filePath,
        workspace_id: 'general',
        workflow_type: 'whiteboard_ocr',
      }),
    )
  }

  function submitWorkflowyWrite(event: FormEvent) {
    event.preventDefault()
    runAction(() =>
      postJson('/workflowy/write', {
        parent_node_id: workflowyParent,
        content: workflowyContent,
        workspace_id: 'workflowy',
      }),
    )
  }

  async function runSearch(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    try {
      const response = await apiFetch(
        `/retrieval/search?query=${encodeURIComponent(retrievalQuery)}&top_k=5`,
      )
      if (!response.ok) throw new Error(await response.text())
      const payload = await response.json()
      setSearchHits(payload.hits ?? [])
      setOutput(JSON.stringify(payload, null, 2))
    } catch (error) {
      setOutput(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Local-first durable agent OS</p>
          <h1>Operator Console</h1>
        </div>
        <button
          className="iconButton"
          type="button"
          onClick={() => void refresh()}
          title="Refresh dashboard"
        >
          <RefreshCcw size={18} />
        </button>
      </header>

      <section className="metricBand" aria-label="System summary">
        <Metric icon={<Activity />} label="Workflows" value={dashboard.workflow_count} tone={statusTone} />
        <Metric icon={<AlertTriangle />} label="Manual review" value={dashboard.manual_review_queue_depth} tone="warn" />
        <Metric icon={<Database />} label="Embedding chunks" value={dashboard.embedding_chunk_count} tone="neutral" />
        <Metric icon={<ShieldCheck />} label="Egress writes" value={dashboard.egress_write_count} tone="ok" />
      </section>

      {/* A different project is a different subject, so both lanes start over. */}
      <ProjectCockpit
        key={selectedProject}
        projects={projects}
        selectedProject={selectedProject}
        onSelectProject={setSelectedProject}
      />

      {/* Governed work is its own subject: a WorkUnit spans phases and milestones
          that no single project lease describes, and switching WorkUnit restarts
          both of its lanes for the same reason. */}
      <WorkUnitCockpit
        key={selectedWorkUnit}
        selectedWorkUnit={selectedWorkUnit}
        onSelectWorkUnit={setSelectedWorkUnit}
      />

      <section className="workgrid">
        <div className="panel span2">
          <div className="panelHeader">
            <h2>Recent Workflow Runs</h2>
            <span>{dashboard.deduped_egress_count} deduped writes</span>
          </div>
          <div className="tableWrap">
            <table>
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Workspace</th>
                  <th>Status</th>
                  <th>Stage</th>
                </tr>
              </thead>
              <tbody>
                {(workflows.length ? workflows : dashboard.recent_workflows).map((row) => (
                  <tr key={row.workflow_id}>
                    <td title={row.workflow_id}>{row.workflow_type}</td>
                    <td>{row.workspace_id}</td>
                    <td>
                      <span className={`pill ${row.status.toLowerCase()}`}>{row.status}</span>
                    </td>
                    <td>{row.current_stage}</td>
                  </tr>
                ))}
                {!workflows.length && !dashboard.recent_workflows.length && (
                  <tr>
                    <td colSpan={4}>No workflow runs yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <form className="panel" onSubmit={submitQuestion}>
          <div className="panelHeader">
            <h2>General Question</h2>
            <Brain size={18} />
          </div>
          <textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={5} />
          <button className="command" type="submit" disabled={busy || !question.trim()}>
            <Play size={16} /> Run
          </button>
        </form>

        <form className="panel" onSubmit={submitFile}>
          <div className="panelHeader">
            <h2>File Ingress</h2>
            <FileInput size={18} />
          </div>
          <input
            value={filePath}
            onChange={(event) => setFilePath(event.target.value)}
            placeholder="/absolute/path/to/image.png"
          />
          <button className="command" type="submit" disabled={busy || !filePath.trim()}>
            <Play size={16} /> OCR
          </button>
        </form>

        <form className="panel" onSubmit={runSearch}>
          <div className="panelHeader">
            <h2>Retrieval</h2>
            <Search size={18} />
          </div>
          <input value={retrievalQuery} onChange={(event) => setRetrievalQuery(event.target.value)} />
          <button className="command" type="submit" disabled={busy || !retrievalQuery.trim()}>
            <Search size={16} /> Search
          </button>
          <div className="hitList">
            {searchHits.map((hit) => (
              <p key={hit.chunk_id}>
                <strong>{hit.score.toFixed(3)}</strong> {hit.text.slice(0, 180)}
              </p>
            ))}
          </div>
        </form>

        <form className="panel" onSubmit={submitWorkflowyWrite}>
          <div className="panelHeader">
            <h2>Workflowy Egress</h2>
            <ShieldCheck size={18} />
          </div>
          <input
            value={workflowyParent}
            onChange={(event) => setWorkflowyParent(event.target.value)}
            placeholder="approved parent node id"
          />
          <textarea
            value={workflowyContent}
            onChange={(event) => setWorkflowyContent(event.target.value)}
            rows={4}
            placeholder="content to insert"
          />
          <button
            className="command"
            type="submit"
            disabled={busy || !workflowyParent.trim() || !workflowyContent.trim()}
          >
            <Play size={16} /> Write
          </button>
        </form>

        <div className="panel outputPanel span2">
          <div className="panelHeader">
            <h2>Last Result</h2>
            <span>{busy ? 'Running' : 'Idle'}</span>
          </div>
          <pre>{output}</pre>
        </div>
      </section>
    </main>
  )
}

function Metric({
  icon,
  label,
  value,
  tone,
}: {
  icon: ReactNode
  label: string
  value: number
  tone: 'ok' | 'warn' | 'danger' | 'neutral'
}) {
  return (
    <div className={`metric ${tone}`}>
      <span className="metricIcon">{icon}</span>
      <span>{label}</span>
      <strong>{value.toLocaleString()}</strong>
    </div>
  )
}

export default App
