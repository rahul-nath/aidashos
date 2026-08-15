// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

import { CheckCircle2, Clock3, Copy, Timer, Volume2, X } from 'lucide-react'
import type { ReactNode } from 'react'
import { useEffect, useMemo, useState } from 'react'

import { Badge } from './components/ui/badge'
import { Button } from './components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from './components/ui/card'
import { Progress } from './components/ui/progress'
import { Separator } from './components/ui/separator'

type TimerConfig = {
  durationMs: number
  endsAtMs: number
  startedAtMs: number
}

function TimerView() {
  const config = useMemo(() => readTimerConfig(), [])
  const [now, setNow] = useState(() => Date.now())
  const [copied, setCopied] = useState(false)
  const remainingMs = Math.max(0, config.endsAtMs - now)
  const elapsedMs = Math.max(0, Math.min(config.durationMs, now - config.startedAtMs))
  const progress = Math.min(100, Math.max(0, (elapsedMs / config.durationMs) * 100))
  const isDone = remainingMs <= 0

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(id)
  }, [])

  async function copyUrl() {
    await navigator.clipboard.writeText(window.location.href)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }

  return (
    <main className="min-h-svh bg-slate-100 text-slate-950">
      <div className="mx-auto flex min-h-svh w-full max-w-5xl items-center px-4 py-6 sm:px-8">
        <Card className="w-full overflow-hidden">
          <CardHeader className="border-b border-slate-200 bg-white">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <CardDescription className="mb-2 flex items-center gap-2 font-semibold uppercase tracking-normal">
                  <Timer className="h-4 w-4 text-teal-700" />
                  Pi Timer
                </CardDescription>
                <CardTitle className="text-2xl sm:text-3xl">
                  {formatDuration(config.durationMs)}
                </CardTitle>
              </div>
              <Badge variant={isDone ? 'success' : 'warning'}>{isDone ? 'Ringing' : 'Armed'}</Badge>
            </div>
          </CardHeader>

          <CardContent className="space-y-8 p-6 sm:p-8">
            <section aria-live="polite" className="space-y-4">
              <div className="flex items-center gap-3 text-slate-500">
                {isDone ? (
                  <CheckCircle2 className="h-5 w-5 text-emerald-700" />
                ) : (
                  <Clock3 className="h-5 w-5 text-teal-700" />
                )}
                <span className="text-sm font-semibold uppercase tracking-normal">
                  {isDone ? 'Time elapsed' : 'Remaining'}
                </span>
              </div>
              <div className="font-mono text-6xl font-semibold leading-none tracking-normal sm:text-8xl">
                {formatRemaining(remainingMs)}
              </div>
              <Progress value={isDone ? 100 : progress} />
            </section>

            <Separator />

            <section className="grid gap-3 sm:grid-cols-3">
              <TimerDatum label="Started" value={formatClock(config.startedAtMs)} />
              <TimerDatum label="Alarm" value={formatClock(config.endsAtMs)} />
              <TimerDatum
                label="Audio"
                value="System"
                icon={<Volume2 className="h-4 w-4 text-teal-700" />}
              />
            </section>
          </CardContent>

          <CardFooter className="flex flex-wrap justify-between gap-3 border-t border-slate-200 bg-slate-50 p-4 sm:p-6">
            <Button type="button" variant="outline" onClick={copyUrl}>
              <Copy className="h-4 w-4" />
              {copied ? 'Copied' : 'Copy URL'}
            </Button>
            <Button type="button" variant="ghost" onClick={() => window.close()}>
              <X className="h-4 w-4" />
              Close
            </Button>
          </CardFooter>
        </Card>
      </div>
    </main>
  )
}

function TimerDatum({
  icon,
  label,
  value,
}: {
  icon?: ReactNode
  label: string
  value: string
}) {
  return (
    <div className="flex min-h-20 items-center justify-between gap-3 rounded-md border border-slate-200 bg-white px-4 py-3">
      <div className="min-w-0">
        <div className="text-xs font-semibold uppercase tracking-normal text-slate-500">{label}</div>
        <div className="truncate font-mono text-lg font-semibold tracking-normal">{value}</div>
      </div>
      {icon}
    </div>
  )
}

function readTimerConfig(): TimerConfig {
  const params = new URLSearchParams(window.location.search)
  const now = Date.now()
  const durationSeconds = readPositiveNumber(params.get('duration')) ?? 50 * 60
  const startedAtMs = readPositiveNumber(params.get('startedAt')) ?? now
  const endsAtMs = readPositiveNumber(params.get('endsAt')) ?? startedAtMs + durationSeconds * 1000
  return {
    durationMs: Math.max(1000, durationSeconds * 1000),
    endsAtMs,
    startedAtMs,
  }
}

function readPositiveNumber(value: string | null): number | null {
  if (value === null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

function formatRemaining(ms: number) {
  const totalSeconds = Math.ceil(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) {
    return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
  }
  return [minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
}

function formatDuration(ms: number) {
  const minutes = Math.round(ms / 60000)
  if (minutes >= 60 && minutes % 60 === 0) {
    const hours = minutes / 60
    return hours === 1 ? '1 hour timer' : `${hours} hour timer`
  }
  return minutes === 1 ? '1 minute timer' : `${minutes} minute timer`
}

function formatClock(ms: number) {
  return new Intl.DateTimeFormat(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(ms))
}

export default TimerView
