import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { ActivityEntry, ActivityLevel, SchedulerStatus } from '../api/types'
import { usePolling } from '../hooks/usePolling'
import { Button, EmptyState, ErrorBanner, Spinner } from './ui'
const LIVE_INTERVAL_MS = 4000
const MAX_KEPT = 2000
const LEVEL_STYLES: Record<ActivityLevel, string> = {
  info: 'bg-slate-100 text-slate-700',
  warning: 'bg-amber-100 text-amber-800',
  error: 'bg-red-100 text-red-800',
}
export function formatTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}
function ago(seconds: number): string {
  if (seconds < 90) return `${seconds}s ago`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} h ago`
}
export function SchedulerBanner({ status }: { status: SchedulerStatus | null }) {
  if (!status) return null
  if (status.last_tick === null) {
    return (
      <div className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800 ring-1 ring-inset ring-amber-200">
        The scheduler hasn&apos;t recorded a check yet. If it should be running, check the scheduler log on
        the Logs page.
      </div>
    )
  }
  const secs = status.interval_seconds
  const every = secs ? ` (checks every ${secs < 60 ? `${secs} s` : `${Math.round(secs / 60)} min`})` : ''
  const counts =
    status.instances_checked !== undefined
      ? ` — last check: ${status.instances_checked} instance(s), ${status.fired ?? 0} action(s), ${status.failed ?? 0} failed`
      : ''
  if (status.running) {
    return (
      <div className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
        Scheduler running — last checked {ago(status.age_seconds ?? 0)}
        {every}
        {counts}
      </div>
    )
  }
  return (
    <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
      Scheduler not checking in — last check {ago(status.age_seconds ?? 0)}
      {every}. Scheduled starts and stops won&apos;t happen until it runs again. See the scheduler log on the
      Logs page.
    </div>
  )
}
export function ActivityFeed({
  name,
  groupName,
  compact = false,
  onScheduler,
}: {
  name?: string
  groupName?: string
  compact?: boolean
  onScheduler?: (status: SchedulerStatus) => void
}) {
  const [entries, setEntries] = useState<ActivityEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState<ActivityLevel | ''>('')
  const [source, setSource] = useState('')
  const [query, setQuery] = useState('')
  const [appliedQuery, setAppliedQuery] = useState('')
  const [live, setLive] = useState(true)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [hasOlder, setHasOlder] = useState(true)
  const seqRef = useRef(0)
  const entriesRef = useRef<ActivityEntry[] | null>(null)
  const onSchedulerRef = useRef(onScheduler)
  useEffect(() => {
    onSchedulerRef.current = onScheduler
  })
  const pageSize = compact ? 50 : 200
  const filters = useCallback(
    () => ({
      name,
      groupName,
      level: level || undefined,
      source: source || undefined,
      q: appliedQuery || undefined,
    }),
    [name, groupName, level, source, appliedQuery],
  )
  useEffect(() => {
    const seq = ++seqRef.current
    setEntries(null)
    setHasOlder(true)
    api
      .getActivity({ ...filters(), limit: pageSize })
      .then((r) => {
        if (seq !== seqRef.current) return
        entriesRef.current = r.entries
        setEntries(r.entries)
        setHasOlder(r.entries.length >= pageSize)
        setError(null)
        onSchedulerRef.current?.(r.scheduler)
      })
      .catch((e) => {
        if (seq === seqRef.current) setError(e instanceof ApiError ? e.message : 'Could not load activity.')
      })
  }, [filters, pageSize])
  const pollNew = useCallback(() => {
    const current = entriesRef.current
    if (current === null) return
    const seq = seqRef.current
    const afterId = current.length ? current[0].id : undefined
    api
      .getActivity({ ...filters(), afterId, limit: 500 })
      .then((r) => {
        if (seq !== seqRef.current) return
        onSchedulerRef.current?.(r.scheduler)
        if (r.entries.length === 0) return
        const merged = [...r.entries, ...(entriesRef.current ?? [])].slice(0, MAX_KEPT)
        entriesRef.current = merged
        setEntries(merged)
        setError(null)
      })
      .catch(() => {})
  }, [filters])
  usePolling(pollNew, LIVE_INTERVAL_MS, live)
  const loadOlder = async () => {
    const current = entriesRef.current
    if (!current || current.length === 0 || loadingOlder) return
    const seq = seqRef.current
    setLoadingOlder(true)
    try {
      const r = await api.getActivity({
        ...filters(),
        beforeId: current[current.length - 1].id,
        limit: pageSize,
      })
      if (seq !== seqRef.current) return
      const merged = [...(entriesRef.current ?? []), ...r.entries]
      entriesRef.current = merged
      setEntries(merged)
      setHasOlder(r.entries.length >= pageSize)
    } catch (e) {
      if (seq === seqRef.current)
        setError(e instanceof ApiError ? e.message : 'Could not load older activity.')
    } finally {
      if (seq === seqRef.current) setLoadingOlder(false)
    }
  }
  const selectClass =
    'rounded-md border-0 py-1.5 pl-2 pr-7 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Level"
          className={selectClass}
          value={level}
          onChange={(e) => setLevel(e.target.value as ActivityLevel | '')}
        >
          <option value="">All levels</option>
          <option value="warning">Warnings and errors</option>
          <option value="error">Errors only</option>
        </select>
        <select
          aria-label="Source"
          className={selectClass}
          value={source}
          onChange={(e) => setSource(e.target.value)}
        >
          <option value="">All sources</option>
          <option value="scheduler">Scheduler decisions</option>
          <option value="schedule">Scheduled actions</option>
          <option value="manual">Manual (CLI)</option>
          <option value="api">Dashboard / API</option>
          <option value="console">Console commands</option>
        </select>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            setAppliedQuery(query.trim())
          }}
        >
          <input
            aria-label="Search messages"
            className="w-56 rounded-md border-0 py-1.5 px-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
            placeholder="Search messages"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Button variant="secondary" type="submit">
            Search
          </Button>
        </form>
        <label className="ml-auto flex items-center gap-2 text-sm text-slate-600">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
          Live
        </label>
      </div>
      {error && <ErrorBanner message={error} />}
      {entries === null ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : entries.length === 0 ? (
        <EmptyState
          title="No activity yet"
          subtitle="Starts, stops, scheduler decisions and console commands appear here."
        />
      ) : (
        <div className="overflow-x-auto rounded-md ring-1 ring-slate-200">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
              <tr>
                <th className="whitespace-nowrap px-3 py-2">Time</th>
                <th className="px-3 py-2">Level</th>
                <th className="px-3 py-2">Source</th>
                {!name && <th className="px-3 py-2">Instance</th>}
                <th className="px-3 py-2">Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {entries.map((e) => (
                <tr key={e.id} className="align-top">
                  <td className="whitespace-nowrap px-3 py-1.5 text-xs text-slate-500">
                    {formatTime(e.timestamp)}
                  </td>
                  <td className="px-3 py-1.5">
                    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${LEVEL_STYLES[e.level]}`}>
                      {e.level}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-3 py-1.5 text-xs text-slate-600">
                    {e.source}
                    {e.actor ? <span className="block text-slate-400">{e.actor}</span> : null}
                  </td>
                  {!name && (
                    <td className="whitespace-nowrap px-3 py-1.5 text-xs">
                      {e.instance_name ? (
                        <Link
                          className="text-indigo-600 hover:underline"
                          to={`/instances/${encodeURIComponent(e.instance_name)}`}
                        >
                          {e.instance_name}
                        </Link>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                  )}
                  <td className="px-3 py-1.5 font-mono text-xs whitespace-pre-wrap break-words text-slate-800">
                    {e.message}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {entries !== null && entries.length > 0 && hasOlder && (
        <div className="flex justify-center">
          <Button variant="secondary" disabled={loadingOlder} onClick={loadOlder}>
            {loadingOlder ? 'Loading…' : 'Load older'}
          </Button>
        </div>
      )}
    </div>
  )
}
