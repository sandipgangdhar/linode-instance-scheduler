import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import type { ScheduleGroup } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
import { useStatusBar } from '../status/StatusBarContext'
function sameSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x) => b.includes(x))
}
function GroupLinks({ names }: { names: string[] }) {
  return (
    <>
      {names.map((d, i) => (
        <span key={d}>
          {i > 0 && ', '}
          <Link to={`/groups/${encodeURIComponent(d)}`} className="text-indigo-600 hover:underline">
            {d}
          </Link>
        </span>
      ))}
    </>
  )
}
export function DependencyCard({ group, onChanged }: { group: ScheduleGroup; onChanged: () => void }) {
  const statusBar = useStatusBar()
  const current = group.depends_on ?? []
  const [otherGroups, setOtherGroups] = useState<string[] | null>(null)
  const [chosen, setChosen] = useState<string[]>(current)
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
  const currentKey = current.join('\n')
  useEffect(() => {
    if (!savingRef.current) setChosen(currentKey ? currentKey.split('\n') : [])
  }, [currentKey])
  const toggle = (name: string) =>
    setChosen((c) => (c.includes(name) ? c.filter((x) => x !== name) : [...c, name].sort()))
  const save = async () => {
    if (savingRef.current) return
    savingRef.current = true
    setSaving(true)
    setError(null)
    setWarnings(null)
    const target = [...chosen].sort()
    try {
      const result = await statusBar.run(
        target.length
          ? `Making “${group.name}” depend on ${target.map((t) => `“${t}”`).join(', ')}`
          : `Clearing “${group.name}”'s dependencies`,
        () => api.setGroupDependencies(group.name, target),
      )
      if (!mountedRef.current) return
      if (result.warnings) setWarnings(result.warnings)
      onChanged()
    } catch (e) {
      if (!mountedRef.current) return
      setWarnings(errorWarnings(e))
      setError(e instanceof ApiError ? e.message : 'Could not update the dependencies.')
    } finally {
      if (mountedRef.current) setSaving(false)
      setTimeout(() => {
        savingRef.current = false
      }, 600)
    }
  }
  const options = otherGroups === null ? null : [...new Set([...otherGroups, ...current])].sort()
  const dependents = group.dependents ?? []
  return (
    <Card>
      <CardHeader
        title="Start order"
        subtitle="Members of this group start only after every member of every chosen group is running and ready (post-start hooks included), and those groups' members stop only after this group's are down."
      />
      <div className="space-y-3 px-5 py-4 text-sm">
        {warnings && <WarningBanner messages={warnings} />}
        {error && <ErrorBanner message={error} />}
        <fieldset disabled={saving || options === null}>
          <legend className="text-slate-600">Depends on</legend>
          {options === null ? (
            <p className="mt-1 text-slate-400">Loading groups…</p>
          ) : options.length === 0 ? (
            <p className="mt-1 text-slate-500">There are no other groups to depend on.</p>
          ) : (
            <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1">
              {options.map((n) => (
                <label key={n} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    aria-label={`Depends on ${n}`}
                    checked={chosen.includes(n)}
                    onChange={() => toggle(n)}
                  />
                  <span className="text-slate-800">{n}</span>
                </label>
              ))}
            </div>
          )}
        </fieldset>
        <Button
          variant="primary"
          aria-label="Save start order"
          disabled={saving || options === null || sameSet(chosen, current)}
          onClick={() => void save()}
        >
          {saving ? 'Saving…' : 'Save'}
        </Button>
        <p className="text-slate-500">
          {current.length === 0 ? (
            'Starts without waiting for another group.'
          ) : (
            <>
              Starts after <GroupLinks names={current} />.
            </>
          )}{' '}
          {dependents.length === 0 ? (
            'No other group waits for this one.'
          ) : (
            <>
              Waited on by <GroupLinks names={dependents} />.
            </>
          )}
        </p>
      </div>
    </Card>
  )
}
