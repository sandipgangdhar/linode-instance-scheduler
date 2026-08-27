import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import type { InstanceRecord, Savings, ScheduleGroup, ScheduleGroupSummary } from '../api/types'
import { SavingsCard } from '../components/SavingsCard'
import { ScheduleEditor } from '../components/ScheduleEditor'
import {
  Button,
  Card,
  CardHeader,
  ErrorBanner,
  Spinner,
  typedConfirmationMatches,
  WarningBanner,
} from '../components/ui'
import { usePolling } from '../hooks/usePolling'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
const POLL_INTERVAL_MS = 8000
export function GroupDetailPage() {
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const statusBar = useStatusBar()
  const [group, setGroup] = useState<ScheduleGroup | null>(null)
  const [savings, setSavings] = useState<Savings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [scheduleError, setScheduleError] = useState<string | null>(null)
  const [scheduleWarnings, setScheduleWarnings] = useState<string[] | null>(null)
  const [addError, setAddError] = useState<string | null>(null)
  const [addWarnings, setAddWarnings] = useState<string[] | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteConfirming, setDeleteConfirming] = useState(false)
  const [deleteConfirmName, setDeleteConfirmName] = useState('')
  const [allInstances, setAllInstances] = useState<Record<string, InstanceRecord> | null>(null)
  const [allGroups, setAllGroups] = useState<ScheduleGroupSummary[] | null>(null)
  const [selectedToAdd, setSelectedToAdd] = useState<Set<string>>(new Set())
  const [adding, setAdding] = useState(false)
  const currentNameRef = useRef<string | null>(name)
  useEffect(() => {
    currentNameRef.current = name
    setSelectedToAdd(new Set())
    setDeleteConfirming(false)
    setDeleteConfirmName('')
    setDeleteError(null)
    setAddError(null)
    setAddWarnings(null)
    setScheduleError(null)
    setScheduleWarnings(null)
    setBusy(false)
    setDeleting(false)
    setAdding(false)
    setGroup(null)
    setSavings(null)
    return () => {
      currentNameRef.current = null
    }
  }, [name])
  const reload = useCallback(() => {
    const requestedName = name
    Promise.all([api.getGroup(requestedName), api.getGroupSavings(requestedName).catch(() => null)])
      .then(([g, s]) => {
        if (requestedName !== currentNameRef.current) return
        setGroup(g)
        setSavings(s)
        setError(null)
      })
      .catch((e) => {
        if (requestedName !== currentNameRef.current) return
        setError(e instanceof ApiError ? e.message : 'Could not load this group.')
      })
  }, [name])
  const pickerDataSeq = useRef(0)
  const loadPickerData = useCallback(() => {
    const seq = ++pickerDataSeq.current
    Promise.all([api.listInstances(), api.listGroups()])
      .then(([instances, groups]) => {
        if (seq !== pickerDataSeq.current) return
        setAllInstances(instances)
        setAllGroups(groups)
      })
      .catch(() => {
        if (seq !== pickerDataSeq.current) return
        setAllInstances({})
        setAllGroups([])
      })
  }, [])
  useEffect(() => reload(), [reload])
  useEffect(() => loadPickerData(), [loadPickerData])
  usePolling(reload, POLL_INTERVAL_MS, !busy && !deleting && !adding)
  const saveSchedule = async (schedule: {
    timezone: string
    rules: ScheduleGroup['rules']
    enabled: boolean
  }) => {
    const requestedName = name
    setBusy(true)
    setScheduleError(null)
    setScheduleWarnings(null)
    try {
      const result = await statusBar.run(`Saving schedule for “${requestedName}”`, () =>
        api.setGroupSchedule(requestedName, schedule),
      )
      if (requestedName !== currentNameRef.current) return
      if (result.warnings) setScheduleWarnings(result.warnings)
      reload()
    } catch (e) {
      if (requestedName !== currentNameRef.current) return
      setScheduleWarnings(errorWarnings(e))
      setScheduleError(e instanceof ApiError ? e.message : 'Could not save the group schedule.')
    } finally {
      if (requestedName === currentNameRef.current) setBusy(false)
    }
  }
  const deleteGroup = async () => {
    const requestedName = name
    if (group && group.members.length > 0) {
      setDeleteError(
        `'${requestedName}' now has ${group.members.length} member(s) -- remove them first before deleting the group.`,
      )
      setDeleteConfirming(false)
      return
    }
    setDeleting(true)
    try {
      await statusBar.run(`Deleting group “${requestedName}”`, () => api.deleteGroup(requestedName))
      if (requestedName === currentNameRef.current) navigate('/groups')
    } catch (e) {
      if (requestedName !== currentNameRef.current) return
      setDeleteError(e instanceof ApiError ? e.message : 'Could not delete this group.')
      setDeleting(false)
    }
  }
  const groupNameById = useMemo(() => new Map((allGroups ?? []).map((g) => [g.id, g.name])), [allGroups])
  const candidates = useMemo(() => {
    if (!allInstances || !group) return []
    return Object.entries(allInstances)
      .filter(([, rec]) => rec.group_id !== group.id)
      .map(([instName, rec]) => ({
        name: instName,
        currentGroupName: rec.group_id !== null ? (groupNameById.get(rec.group_id) ?? null) : null,
      }))
      .sort((a, b) => a.name.localeCompare(b.name))
  }, [allInstances, group, groupNameById])
  const toggleSelected = (instName: string) => {
    setSelectedToAdd((prev) => {
      const next = new Set(prev)
      if (next.has(instName)) next.delete(instName)
      else next.add(instName)
      return next
    })
  }
  const addSelected = async () => {
    if (selectedToAdd.size === 0) return
    const targets = Array.from(selectedToAdd)
    const requestedName = name
    setAdding(true)
    setAddError(null)
    setAddWarnings(null)
    try {
      await statusBar.run(
        `Adding ${targets.length} instance${targets.length === 1 ? '' : 's'} to “${requestedName}”`,
        async () => {
          const results = await Promise.allSettled(
            targets.map((instName) => api.patchInstanceGroup(instName, { group_name: requestedName })),
          )
          const failed = results
            .map((r, i) => ({ r, instName: targets[i] }))
            .filter(({ r }) => r.status === 'rejected')
          const warned = results
            .map((r, i) => ({ r, instName: targets[i] }))
            .filter(
              (
                x,
              ): x is {
                r: PromiseFulfilledResult<Awaited<ReturnType<typeof api.patchInstanceGroup>>>
                instName: string
              } => x.r.status === 'fulfilled' && !!x.r.value.warnings && x.r.value.warnings.length > 0,
            )
          if (warned.length > 0 && requestedName === currentNameRef.current) {
            setAddWarnings(
              warned.flatMap(({ r, instName }) => r.value.warnings!.map((w) => `${instName}: ${w}`)),
            )
          }
          if (failed.length > 0) {
            throw new Error(
              `Added ${targets.length - failed.length} of ${targets.length}. Failed: ` +
                failed
                  .map(({ r, instName }) => {
                    const reason = (r as PromiseRejectedResult).reason
                    const message = reason instanceof ApiError ? reason.message : 'unknown error'
                    return `${instName} (${message})`
                  })
                  .join('; '),
            )
          }
        },
      )
    } catch (e) {
      if (requestedName !== currentNameRef.current) return
      setAddError(e instanceof Error ? e.message : 'Could not add the selected instances.')
    } finally {
      if (requestedName === currentNameRef.current) {
        setSelectedToAdd(new Set())
        reload()
      }
      loadPickerData()
      if (requestedName === currentNameRef.current) setAdding(false)
    }
  }
  if (error) {
    return (
      <div className="p-8">
        <ErrorBanner message={error} />
      </div>
    )
  }
  if (!group) {
    return (
      <div className="flex justify-center py-24">
        <Spinner className="text-indigo-600" />
      </div>
    )
  }
  return (
    <div>
      <PageHeader
        title={group.name}
        subtitle={
          <Link to="/groups" className="text-indigo-600 hover:underline">
            ← All groups
          </Link>
        }
        action={
          !deleteConfirming ? (
            <Button
              variant="danger"
              disabled={group.members.length > 0 || deleting}
              title={group.members.length > 0 ? 'Remove every member first' : undefined}
              onClick={() => setDeleteConfirming(true)}
            >
              Delete group
            </Button>
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">Type “{name}” to confirm</span>
              <input
                autoFocus
                className="rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-red-600"
                value={deleteConfirmName}
                onChange={(e) => setDeleteConfirmName(e.target.value)}
              />
              <Button
                variant="danger"
                disabled={
                  !typedConfirmationMatches(deleteConfirmName, name) || deleting || group.members.length > 0
                }
                onClick={deleteGroup}
              >
                {deleting ? 'Deleting…' : 'Confirm delete'}
              </Button>
              <Button
                variant="ghost"
                disabled={deleting}
                onClick={() => {
                  setDeleteConfirming(false)
                  setDeleteConfirmName('')
                }}
              >
                Cancel
              </Button>
            </div>
          )
        }
      />
      <div className="grid grid-cols-3 gap-6 p-8">
        <div className="col-span-2 space-y-6">
          {deleteError && <ErrorBanner message={deleteError} />}
          <Card>
            <CardHeader title="Schedule" subtitle="Applies to every current member below." />
            <div className="space-y-3 px-5 py-4">
              {scheduleWarnings && <WarningBanner messages={scheduleWarnings} />}
              {scheduleError && <ErrorBanner message={scheduleError} />}
              <ScheduleEditor
                key={JSON.stringify({ timezone: group.timezone, rules: group.rules, enabled: group.enabled })}
                initial={{ timezone: group.timezone, rules: group.rules, enabled: group.enabled }}
                saving={busy}
                onSave={saveSchedule}
              />
            </div>
          </Card>
        </div>
        <div className="space-y-6">
          <Card>
            <CardHeader title="Savings" />
            <div className="px-5 py-4">
              <SavingsCard savings={savings} />
            </div>
          </Card>
          <Card>
            <CardHeader title={`Members (${group.members.length})`} />
            {group.members.length === 0 ? (
              <p className="px-5 py-6 text-sm text-slate-500">No members yet — add some below.</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {group.members.map((m) => (
                  <li key={m} className="px-5 py-3 text-sm">
                    <Link
                      to={`/instances/${encodeURIComponent(m)}`}
                      className="text-indigo-600 hover:underline"
                    >
                      {m}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Add instances"
              subtitle="Select any number and add them in one go -- each succeeds or fails independently."
              action={
                <Button variant="primary" disabled={selectedToAdd.size === 0 || adding} onClick={addSelected}>
                  {adding ? 'Adding…' : `Add ${selectedToAdd.size || ''}`.trim()}
                </Button>
              }
            />
            {(addWarnings || addError) && (
              <div className="space-y-2 px-5 pt-3">
                {addWarnings && <WarningBanner messages={addWarnings} />}
                {addError && <ErrorBanner message={addError} />}
              </div>
            )}
            {candidates.length === 0 ? (
              <p className="px-5 py-6 text-sm text-slate-500">
                Every onboarded instance already belongs to this group.
              </p>
            ) : (
              <ul className="max-h-72 divide-y divide-slate-100 overflow-y-auto">
                {candidates.map((c) => (
                  <li key={c.name} className="flex items-center gap-2.5 px-5 py-2.5 text-sm">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-600"
                      checked={selectedToAdd.has(c.name)}
                      disabled={adding}
                      onChange={() => toggleSelected(c.name)}
                    />
                    <span className="flex-1 truncate">{c.name}</span>
                    {c.currentGroupName && (
                      <span className="shrink-0 text-xs text-slate-400">
                        moves from “{c.currentGroupName}”
                      </span>
                    )}
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
