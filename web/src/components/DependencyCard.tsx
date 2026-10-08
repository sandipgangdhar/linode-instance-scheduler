import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import type { ScheduleGroup } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
import { useStatusBar } from '../status/StatusBarContext'
export function DependencyCard({ group, onChanged }: { group: ScheduleGroup; onChanged: () => void }) {
  const statusBar = useStatusBar()
  const [otherGroups, setOtherGroups] = useState<string[] | null>(null)
  const [choice, setChoice] = useState<string>(group.depends_on ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[] | null>(null)
  const savingRef = useRef(false)
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    api
      .listGroups()
      .then((gs) => {
        if (mountedRef.current) setOtherGroups(gs.map((g) => g.name).filter((n) => n !== group.name))
      })
      .catch(() => {
        if (mountedRef.current) setOtherGroups([])
      })
    return () => {
      mountedRef.current = false
    }
  }, [group.name])
  useEffect(() => {
    if (!savingRef.current) setChoice(group.depends_on ?? '')
  }, [group.depends_on])
  const current = group.depends_on ?? ''
  const save = async () => {
    if (savingRef.current) return
    savingRef.current = true
    setSaving(true)
    setError(null)
    setWarnings(null)
    const target = choice === '' ? null : choice
    try {
      const result = await statusBar.run(
        target ? `Making “${group.name}” depend on “${target}”` : `Clearing “${group.name}”'s dependency`,
        () => api.setGroupDependency(group.name, target),
      )
      if (!mountedRef.current) return
      if (result.warnings) setWarnings(result.warnings)
      onChanged()
    } catch (e) {
      if (!mountedRef.current) return
      setWarnings(errorWarnings(e))
      setError(e instanceof ApiError ? e.message : 'Could not update the dependency.')
    } finally {
      if (mountedRef.current) setSaving(false)
      setTimeout(() => {
        savingRef.current = false
      }, 600)
    }
  }
  const dependents = group.dependents ?? []
  return (
    <Card>
      <CardHeader
        title="Start order"
        subtitle="Members of this group start only after every member of the chosen group is running and ready (its post-start hooks included), and that group's members stop only after this group's are down."
      />
      <div className="space-y-3 px-5 py-4 text-sm">
        {warnings && <WarningBanner messages={warnings} />}
        {error && <ErrorBanner message={error} />}
        <div className="flex items-center gap-3">
          <label htmlFor="depends-on" className="text-slate-600">
            Depends on
          </label>
          <select
            id="depends-on"
            aria-label="Depends on group"
            className="rounded-md border border-slate-300 px-2 py-1"
            value={choice}
            disabled={saving || otherGroups === null}
            onChange={(e) => setChoice(e.target.value)}
          >
            <option value="">No dependency</option>
            {(otherGroups ?? []).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
            {current && otherGroups !== null && !otherGroups.includes(current) && (
              <option value={current}>{current}</option>
            )}
          </select>
          <Button
            variant="primary"
            aria-label="Save start order"
            disabled={saving || choice === current}
            onClick={() => void save()}
          >
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </div>
        <p className="text-slate-500">
          {dependents.length === 0 ? (
            'No other group waits for this one.'
          ) : (
            <>
              Waited on by{' '}
              {dependents.map((d, i) => (
                <span key={d}>
                  {i > 0 && ', '}
                  <Link to={`/groups/${encodeURIComponent(d)}`} className="text-indigo-600 hover:underline">
                    {d}
                  </Link>
                </span>
              ))}
              .
            </>
          )}
        </p>
      </div>
    </Card>
  )
}
