import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ConsoleRun } from '../api/types'
import { formatTime } from '../components/ActivityFeed'
import { Button, Card, CardHeader, ErrorBanner } from '../components/ui'
import { usePolling } from '../hooks/usePolling'
import { PageHeader } from './DashboardLayout'
const HISTORY_KEY = 'lis-console-history'
const OUTPUT_INTERVAL_MS = 1000
const EXAMPLES = [
  'list',
  'status --name NAME',
  'history --name NAME',
  'status --name NAME --check-network',
  'group-list',
  'start --name NAME',
  'stop --name NAME --yes',
  'reset-host-key --name NAME --yes',
  'backup',
  'migrate-orphans',
]
function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string').slice(0, 100) : []
  } catch {
    return []
  }
}
function saveHistory(items: string[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 100)))
  } catch {}
}
export function ConsolePage() {
  const [command, setCommand] = useState('')
  const [run, setRun] = useState<ConsoleRun | null>(null)
  const [runs, setRuns] = useState<Omit<ConsoleRun, 'lines'>[]>([])
  const [commands, setCommands] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [history, setHistory] = useState<string[]>(loadHistory)
  const historyPos = useRef(-1)
  const runIdRef = useRef<string | null>(null)
  const outputRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const loadRuns = useCallback(() => {
    api
      .listConsoleRuns()
      .then(setRuns)
      .catch(() => {})
  }, [])
  useEffect(() => {
    api
      .listConsoleCommands()
      .then((r) => setCommands(r.commands))
      .catch((e) => setError(e instanceof ApiError ? e.message : 'The console is not available.'))
    loadRuns()
  }, [loadRuns])
  const show = useCallback(async (id: string) => {
    runIdRef.current = id
    try {
      const view = await api.getConsoleRun(id, 0)
      if (runIdRef.current === id) setRun(view)
    } catch (e) {
      if (runIdRef.current === id) setError(e instanceof ApiError ? e.message : 'Could not load that run.')
    }
  }, [])
  const pollOutput = useCallback(() => {
    const current = run
    if (!current || current.status !== 'running') return
    const id = current.id
    api
      .getConsoleRun(id, current.next_offset)
      .then((view) => {
        if (runIdRef.current !== id) return
        setRun((prev) => (prev && prev.id === id ? { ...view, lines: [...prev.lines, ...view.lines] } : prev))
        if (view.status !== 'running') loadRuns()
      })
      .catch(() => {})
  }, [run, loadRuns])
  usePolling(pollOutput, OUTPUT_INTERVAL_MS, run?.status === 'running')
  useEffect(() => {
    if (outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight
  }, [run?.lines.length])
  const execute = async () => {
    const text = command.trim()
    if (!text || starting) return
    setStarting(true)
    setError(null)
    try {
      const started = await api.runConsoleCommand(text)
      runIdRef.current = started.id
      setRun(started)
      const next = [text, ...history.filter((h) => h !== text)]
      setHistory(next)
      saveHistory(next)
      historyPos.current = -1
      setCommand('')
      loadRuns()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not run the command.')
    } finally {
      setStarting(false)
      inputRef.current?.focus()
    }
  }
  const cancel = async () => {
    if (!run || run.status !== 'running') return
    try {
      const view = await api.cancelConsoleRun(run.id)
      setRun((prev) => (prev && prev.id === view.id ? { ...view, lines: prev.lines } : prev))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not cancel.')
    }
  }
  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      void execute()
    } else if (e.key === 'ArrowUp' && history.length) {
      e.preventDefault()
      historyPos.current = Math.min(historyPos.current + 1, history.length - 1)
      setCommand(history[historyPos.current])
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      historyPos.current = Math.max(historyPos.current - 1, -1)
      setCommand(historyPos.current === -1 ? '' : history[historyPos.current])
    }
  }
  const statusLabel = run
    ? run.status === 'running'
      ? 'running…'
      : run.status === 'cancelled'
        ? 'cancelled'
        : `exit ${run.exit_code}`
    : null
  return (
    <div>
      <PageHeader
        title="Console"
        subtitle="Run this tool's commands on the scheduler host, with live output. Commands that ask for confirmation need --yes. Every command is recorded with who ran it."
      />
      <div className="space-y-4 px-8 py-6">
        {error && <ErrorBanner message={error} />}
        <div className="overflow-hidden rounded-md bg-slate-950 ring-1 ring-slate-800">
          <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2 text-xs text-slate-400">
            <span className="font-mono">
              {run ? `$ ${run.command}` : 'Type a command below, e.g. list or status --name web-1'}
            </span>
            <span className="flex items-center gap-3">
              {statusLabel && (
                <span
                  className={
                    run?.status === 'done'
                      ? 'text-emerald-400'
                      : run?.status === 'running'
                        ? 'text-sky-300'
                        : 'text-red-300'
                  }
                >
                  {statusLabel}
                </span>
              )}
              {run?.status === 'running' && (
                <button
                  onClick={cancel}
                  className="rounded bg-slate-800 px-2 py-0.5 text-slate-200 hover:bg-slate-700"
                >
                  Cancel
                </button>
              )}
            </span>
          </div>
          <div
            ref={outputRef}
            role="log"
            aria-label="Command output"
            className="h-[50vh] overflow-auto p-3 font-mono text-xs leading-5 text-slate-200"
          >
            {run ? (
              run.lines.length ? (
                run.lines.map((line, i) => (
                  <div key={i} className="whitespace-pre-wrap break-words">
                    {line}
                  </div>
                ))
              ) : (
                <span className="text-slate-500">
                  {run.status === 'running' ? 'Waiting for output…' : 'No output.'}
                </span>
              )
            ) : (
              <span className="text-slate-500">Output appears here.</span>
            )}
            {run?.truncated && <div className="text-amber-300">[output truncated]</div>}
          </div>
          <div className="flex items-center gap-2 border-t border-slate-800 px-3 py-2">
            <span className="font-mono text-sm text-emerald-400">$</span>
            <input
              ref={inputRef}
              aria-label="Command"
              autoFocus
              spellCheck={false}
              autoComplete="off"
              className="flex-1 border-0 bg-transparent font-mono text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:ring-0"
              placeholder="status --name web-1"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              onKeyDown={onKeyDown}
            />
            <Button variant="primary" disabled={starting || !command.trim()} onClick={() => void execute()}>
              Run
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader
              title="Examples"
              subtitle="Click to put one in the command line. Replace NAME with an instance name; add --help to any command."
            />
            <div className="flex flex-wrap gap-2 px-5 pb-5">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  className="rounded bg-slate-100 px-2 py-1 font-mono text-xs text-slate-700 hover:bg-slate-200"
                  onClick={() => {
                    setCommand(ex)
                    inputRef.current?.focus()
                  }}
                >
                  {ex}
                </button>
              ))}
            </div>
            {commands.length > 0 && (
              <p className="px-5 pb-5 text-xs text-slate-500">
                Available: <span className="font-mono">{commands.join(', ')}</span>. Long-running services,
                database restore, SSH key backup and creating API tokens aren&apos;t available here.
              </p>
            )}
          </Card>
          <Card>
            <CardHeader title="Recent commands" subtitle="Since the API server last started." />
            {runs.length === 0 ? (
              <p className="px-5 pb-5 text-sm text-slate-500">None yet.</p>
            ) : (
              <ul className="divide-y divide-slate-100 px-5 pb-3">
                {runs.slice(0, 15).map((r) => (
                  <li key={r.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                    <button
                      className="truncate text-left font-mono text-xs text-indigo-600 hover:underline"
                      onClick={() => void show(r.id)}
                    >
                      $ {r.command}
                    </button>
                    <span className="shrink-0 text-xs text-slate-500">
                      {r.actor} · {formatTime(r.started_at)} ·{' '}
                      {r.status === 'running'
                        ? 'running'
                        : r.status === 'cancelled'
                          ? 'cancelled'
                          : `exit ${r.exit_code}`}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}
