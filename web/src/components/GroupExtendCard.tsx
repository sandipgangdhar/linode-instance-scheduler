import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { GroupExtendResult, ScheduleGroup } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner } from './ui'
export function GroupExtendCard({ group, onChanged }: { group: ScheduleGroup; onChanged: () => void }) {
  const [hours, setHours] = useState(2)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<GroupExtendResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const busyRef = useRef(false)
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  const hasSchedule = group.enabled && group.rules.length > 0
  const extend = async () => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const r = await api.extendGroup(group.name, hours)
      if (mountedRef.current) setResult(r)
      onChanged()
    } catch (e) {
      if (mountedRef.current) setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
    } finally {
      busyRef.current = false
      if (mountedRef.current) setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader
        title="Keep running past today's stop"
        subtitle="Every running member that follows this group's schedule skips today's scheduled stop and keeps running for the chosen hours; tomorrow's schedule is unchanged."
      />
      <div className="space-y-3 px-5 py-4 text-sm">
        {!hasSchedule ? (
          <p className="text-slate-500">This group has no active schedule, so nothing stops its members.</p>
        ) : (
          <div className="flex items-center gap-2">
            <select
              aria-label="Extend group by hours"
              className="rounded-md border-0 py-1.5 pl-2 pr-7 text-sm text-slate-900 ring-1 ring-inset ring-slate-300"
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
              disabled={busy}
            >
              {[1, 2, 3, 4, 6, 8, 12].map((h) => (
                <option key={h} value={h}>
                  {h} h
                </option>
              ))}
            </select>
            <Button variant="secondary" disabled={busy} onClick={extend}>
              {busy ? 'Extending…' : 'Extend the group'}
            </Button>
          </div>
        )}
        {(group.depends_on ?? []).length > 0 && hasSchedule && (
          <p className="text-xs text-slate-500">
            Groups it depends on ({(group.depends_on ?? []).join(', ')}) are held for the same time, so they
            stop after it.
          </p>
        )}
        {result && (
          <div className="space-y-1 rounded-md bg-emerald-50 px-3 py-2 text-emerald-900">
            <p>
              Stops at <strong>{new Date(result.stops_at).toLocaleString()}</strong>:{' '}
              {result.extended.length
                ? result.extended.join(', ')
                : 'no running members followed the group schedule'}
              .
            </p>
            {result.dependencies_held.length > 0 && (
              <p className="text-xs">Also held (start order): {result.dependencies_held.join(', ')}</p>
            )}
            {result.skipped.map((s) => (
              <p key={s.name} className="text-xs text-slate-600">
                Skipped {s.name}: {s.reason}
              </p>
            ))}
          </div>
        )}
        {error && <ErrorBanner message={error} />}
      </div>
    </Card>
  )
}
