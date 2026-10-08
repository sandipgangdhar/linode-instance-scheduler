import { useEffect, useRef, useState } from 'react'
import { api, ApiError, errorWarnings } from '../api/client'
import type { GroupActionResult, ScheduleGroup } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
import { useStatusBar } from '../status/StatusBarContext'
export function GroupActionsCard({ group, onChanged }: { group: ScheduleGroup; onChanged: () => void }) {
  const statusBar = useStatusBar()
  const [withDependencies, setWithDependencies] = useState(false)
  const [confirmingStop, setConfirmingStop] = useState(false)
  const [busy, setBusy] = useState<'start' | 'stop' | null>(null)
  const [result, setResult] = useState<GroupActionResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[] | null>(null)
  const busyRef = useRef(false)
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  const hasChain = withDependencies && (group.depends_on || (group.dependents ?? []).length > 0)
  const act = async (action: 'start' | 'stop') => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(action)
    setConfirmingStop(false)
    setError(null)
    setWarnings(null)
    setResult(null)
    try {
      const r = await statusBar.run(
        `${action === 'start' ? 'Starting' : 'Stopping'} group “${group.name}”`,
        (report) =>
          api.groupAction(group.name, action, { withDependencies }, report, (w) => {
            if (mountedRef.current) setWarnings(w)
          }),
      )
      if (!mountedRef.current) return
      setResult(r)
      if (!r.ok) throw new Error('Some members did not ' + action + ' — see the list below.')
    } catch (e) {
      if (!mountedRef.current) return
      setWarnings((w) => w ?? errorWarnings(e))
      setError(
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : `Could not ${action} the group.`,
      )
    } finally {
      if (mountedRef.current) {
        setBusy(null)
        onChanged()
      }
      setTimeout(() => {
        busyRef.current = false
      }, 600)
    }
  }
  return (
    <Card>
      <CardHeader
        title="Run now"
        subtitle="Start or stop every member at once, in parallel, regardless of the schedule."
      />
      <div className="space-y-3 px-5 py-4 text-sm">
        {warnings && <WarningBanner messages={warnings} />}
        {error && <ErrorBanner message={error} />}
        {(group.depends_on || (group.dependents ?? []).length > 0) && (
          <label className="flex items-center gap-2 text-slate-700">
            <input
              type="checkbox"
              checked={withDependencies}
              onChange={(e) => setWithDependencies(e.target.checked)}
            />
            Include the dependency chain (start: the groups this one needs first; stop: the groups that need
            this one first)
          </label>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            disabled={busy !== null || (group.members.length === 0 && !hasChain)}
            onClick={() => void act('start')}
          >
            {busy === 'start' ? 'Starting…' : 'Start group'}
          </Button>
          {confirmingStop ? (
            <>
              <Button variant="danger" disabled={busy !== null} onClick={() => void act('stop')}>
                Confirm stop{hasChain ? ' (with dependents)' : ''}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmingStop(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button
              variant="secondary"
              disabled={busy !== null || (group.members.length === 0 && !hasChain)}
              onClick={() => setConfirmingStop(true)}
            >
              {busy === 'stop' ? 'Stopping…' : 'Stop group'}
            </Button>
          )}
        </div>
        {result && (
          <ul
            aria-label="Group action results"
            className="divide-y divide-slate-100 rounded-md border border-slate-200"
          >
            {result.members.map((m) => (
              <li key={`${m.group}/${m.name}`} className="flex justify-between px-3 py-1.5">
                <span>{m.group === result.group ? m.name : `${m.group} / ${m.name}`}</span>
                <span className={m.ok ? 'text-emerald-700' : 'text-red-700'}>
                  {m.outcome.replaceAll('_', ' ')}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  )
}
