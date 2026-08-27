import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import {
  START_SUCCESS_OUTCOMES,
  STOP_SUCCESS_OUTCOMES,
  type InstanceRecord,
  type Savings,
  type Schedule,
  type ScheduleEvent,
  type ScheduleGroupSummary,
} from '../api/types'
import { SavingsCard } from '../components/SavingsCard'
import { ScheduleEditor } from '../components/ScheduleEditor'
import { StatusBadge } from '../components/StatusBadge'
import {
  Button,
  Card,
  CardHeader,
  ErrorBanner,
  SecurityWarningBanner,
  Spinner,
  TypeToConfirmInput,
  typedConfirmationMatches,
  WarningBanner,
} from '../components/ui'
import { usePolling } from '../hooks/usePolling'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
const POLL_INTERVAL_MS = 8000
class SecurityWarningError extends Error {}
export function InstanceDetailPage() {
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const statusBar = useStatusBar()
  const [record, setRecord] = useState<InstanceRecord | null>(null)
  const [schedule, setSchedule] = useState<Schedule | null>(null)
  const [savings, setSavings] = useState<Savings | null>(null)
  const [history, setHistory] = useState<ScheduleEvent[]>([])
  const [groups, setGroups] = useState<ScheduleGroupSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [securityWarning, setSecurityWarning] = useState<string | null>(null)
  const [actionWarnings, setActionWarnings] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [confirmName, setConfirmName] = useState('')
  const [deleteVolumes, setDeleteVolumes] = useState(false)
  const [offboardError, setOffboardError] = useState<string | null>(null)
  const [offboardErrorWarnings, setOffboardErrorWarnings] = useState<string[] | null>(null)
  const [offboardSuccessWarnings, setOffboardSuccessWarnings] = useState<string[] | null>(null)
  const [stopConfirming, setStopConfirming] = useState(false)
  const [stopConfirmName, setStopConfirmName] = useState('')
  const [pendingGroupRemoval, setPendingGroupRemoval] = useState(false)
  const [deregisterConfirmName, setDeregisterConfirmName] = useState('')
  const [deregisterError, setDeregisterError] = useState<string | null>(null)
  const currentNameRef = useRef<string | null>(name)
  useEffect(() => {
    currentNameRef.current = name
    setDeleteVolumes(false)
    setConfirmName('')
    setStopConfirming(false)
    setStopConfirmName('')
    setPendingGroupRemoval(false)
    setDeregisterConfirmName('')
    setRecord(null)
    setSchedule(null)
    setSavings(null)
    setHistory([])
    setBusy(null)
    setActionError(null)
    setSecurityWarning(null)
    setActionWarnings([])
    setOffboardError(null)
    setOffboardErrorWarnings(null)
    setOffboardSuccessWarnings(null)
    setDeregisterError(null)
    return () => {
      currentNameRef.current = null
    }
  }, [name])
  const reload = useCallback(() => {
    const requestedName = name
    Promise.all([
      api.getInstance(requestedName),
      api.getSchedule(requestedName).catch(() => null),
      api.getInstanceSavings(requestedName).catch(() => null),
      api.getHistory(requestedName, 20).catch(() => []),
    ])
      .then(([r, s, sav, h]) => {
        if (requestedName !== currentNameRef.current) return
        setRecord(r)
        setSchedule(s)
        setSavings(sav)
        setHistory(h)
        setError(null)
      })
      .catch((e) => {
        if (requestedName !== currentNameRef.current) return
        setError(e instanceof ApiError ? e.message : 'Could not load this instance.')
      })
  }, [name])
  useEffect(() => reload(), [reload])
  useEffect(() => {
    api
      .listGroups()
      .then(setGroups)
      .catch(() => setGroups([]))
  }, [name])
  usePolling(reload, POLL_INTERVAL_MS, busy === null && offboardSuccessWarnings === null)
  const run = async (
    label: string,
    fn: (
      onProgress: (percent: number, step: string | null) => void,
      onWarning: (warnings: string[]) => void,
    ) => Promise<unknown>,
  ) => {
    const requestedName = name
    setBusy(label)
    setActionError(null)
    setSecurityWarning(null)
    setActionWarnings([])
    try {
      await statusBar.run(`${capitalize(label)} “${requestedName}”`, (report) =>
        fn(report, (warnings) => {
          if (requestedName === currentNameRef.current) setActionWarnings(warnings)
        }),
      )
      if (requestedName === currentNameRef.current) reload()
    } catch (e) {
      if (requestedName !== currentNameRef.current) return
      const carried = errorWarnings(e)
      if (carried) setActionWarnings((prev) => [...prev, ...carried])
      if (e instanceof SecurityWarningError) {
        setSecurityWarning(e.message)
      } else {
        setActionError(
          e instanceof ApiError ? e.message : e instanceof Error ? e.message : `${label} failed.`,
        )
      }
      reload()
    } finally {
      if (requestedName === currentNameRef.current) setBusy(null)
    }
  }
  function assertStartSucceeded(result: Awaited<ReturnType<typeof api.startInstance>>) {
    if (!START_SUCCESS_OUTCOMES.has(result.outcome)) {
      if (result.security_warning) {
        throw new SecurityWarningError(
          `“${name}” presented a DIFFERENT SSH host key than the one on record` +
            (result.detail ? `: ${result.detail}` : '.') +
            ' This should never happen after a steady-state recreate — do not proceed. If this ' +
            'key change is genuinely expected (e.g. you intentionally replaced the disk), ' +
            'confirm out-of-band first, then reset the trusted host key before retrying.',
        )
      }
      throw new Error(result.detail ?? `Start did not succeed (${result.outcome}).`)
    }
    return result
  }
  function assertStopSucceeded(result: Awaited<ReturnType<typeof api.stopInstance>>) {
    if (!STOP_SUCCESS_OUTCOMES.has(result.outcome)) {
      throw new Error(result.detail ?? `Stop did not succeed (${result.outcome}).`)
    }
    return result
  }
  const offboard = async () => {
    const requestedName = name
    setBusy('offboard')
    setOffboardError(null)
    setOffboardErrorWarnings(null)
    try {
      const result = await statusBar.run(`Offboarding “${name}”`, () =>
        api.offboardInstance(name, deleteVolumes),
      )
      if (requestedName !== currentNameRef.current) return
      if (result.outcome === 'offboarded') {
        if (result.warnings && result.warnings.length > 0) {
          setOffboardSuccessWarnings(result.warnings)
        } else {
          navigate('/instances')
        }
      } else {
        setOffboardError(result.detail ?? 'Offboarding did not complete cleanly.')
      }
    } catch (e) {
      if (requestedName !== currentNameRef.current) return
      setOffboardErrorWarnings(errorWarnings(e))
      setOffboardError(e instanceof ApiError ? e.message : 'Could not offboard this instance.')
    } finally {
      if (requestedName === currentNameRef.current) setBusy(null)
    }
  }
  const deregister = async () => {
    const requestedName = name
    setBusy('deregister')
    setDeregisterError(null)
    try {
      const result = await statusBar.run(`Removing “${name}” from tracking`, () =>
        api.deregisterInstance(name),
      )
      if (requestedName !== currentNameRef.current) return
      if (result.outcome === 'deregistered' || result.outcome === 'not_onboarded') {
        navigate('/instances')
      } else {
        setDeregisterError('Removal did not complete.')
      }
    } catch (e) {
      if (requestedName !== currentNameRef.current) return
      setDeregisterError(e instanceof ApiError ? e.message : 'Could not remove this instance from tracking.')
    } finally {
      if (requestedName === currentNameRef.current) setBusy(null)
    }
  }
  if (error) {
    return (
      <div className="p-8">
        <ErrorBanner message={error} />
      </div>
    )
  }
  if (!record) {
    return (
      <div className="flex justify-center py-24">
        <Spinner className="text-indigo-600" />
      </div>
    )
  }
  const currentGroup = groups.find((g) => g.id === record.group_id)
  const hasActiveIndividualSchedule = schedule !== null && schedule.enabled
  const removeFromGroup = (copy: boolean) => {
    setPendingGroupRemoval(false)
    run('remove from group', async (_onProgress, onWarning) => {
      const result = await api.patchInstanceGroup(name, {
        group_name: null,
        copy_group_rules_as_individual: copy,
      })
      if (result.warnings) onWarning(result.warnings)
      return result
    })
  }
  return (
    <div>
      <PageHeader
        title={name}
        subtitle={
          <Link to="/instances" className="text-indigo-600 hover:underline">
            ← All instances
          </Link>
        }
        action={<StatusBadge status={record.current_status} locked={record.transitioning} />}
      />
      <div className="grid grid-cols-3 gap-6 p-8">
        <div className="col-span-2 space-y-6">
          <Card>
            <CardHeader title="Status" />
            <div className="grid grid-cols-2 gap-4 px-5 py-4 text-sm">
              <Info label="Region" value={record.region} />
              <Info label="Reserved IP" value={record.reserved_ip} />
              <Info label="Linode ID" value={record.current_linode_id?.toString() ?? null} />
              <Info
                label="Group"
                value={currentGroup?.name ?? (record.group_id ? `#${record.group_id}` : null)}
              />
            </div>
            {securityWarning && (
              <div className="px-5 pb-4">
                <SecurityWarningBanner message={securityWarning} />
              </div>
            )}
            {actionError && (
              <div className="px-5 pb-4">
                <ErrorBanner message={actionError} />
              </div>
            )}
            {actionWarnings.length > 0 && (
              <div className="px-5 pb-4">
                <WarningBanner messages={actionWarnings} />
              </div>
            )}
            {record.manual_override_expires_at && (
              <div className="mx-5 mb-4 rounded-md bg-indigo-50 px-4 py-3 text-sm text-indigo-800 ring-1 ring-inset ring-indigo-200">
                Manually started outside its scheduled hours — auto-stops at{' '}
                <strong>{new Date(record.manual_override_expires_at).toLocaleString()}</strong> unless
                extended.
              </div>
            )}
            {record.current_status === 'needs_manual_recovery' && (
              <div className="mx-5 mb-4 rounded-md bg-red-50 px-4 py-3 text-sm text-red-800 ring-1 ring-inset ring-red-200">
                Only partially recovered by `rebuild` -- boot it once manually via Cloud Manager from its
                os_volume_id, then re-run onboard to fully restore management before Start/Stop will work.
              </div>
            )}
            <div className="flex flex-wrap items-start gap-2 px-5 pb-5">
              <Button
                variant="primary"
                disabled={
                  busy !== null ||
                  record.current_status === 'running' ||
                  record.current_status === 'needs_manual_recovery'
                }
                onClick={() =>
                  run('start', (onProgress, onWarning) =>
                    api.startInstance(name, undefined, onProgress, onWarning).then(assertStartSucceeded),
                  )
                }
              >
                {busy === 'start' ? 'Starting…' : 'Start'}
              </Button>
              {!stopConfirming ? (
                <Button
                  variant="secondary"
                  disabled={
                    busy !== null ||
                    record.current_status === 'stopped' ||
                    record.current_status === 'needs_manual_recovery'
                  }
                  onClick={() => setStopConfirming(true)}
                >
                  Stop
                </Button>
              ) : (
                <div className="flex w-full flex-col gap-2 rounded-md bg-amber-50 p-3 ring-1 ring-inset ring-amber-200">
                  <p className="text-xs text-amber-800">
                    Stopping deletes the underlying Linode instance (billing stops; the OS/data volumes and
                    reserved IP survive). Type “{name}” to confirm.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <input
                      autoFocus
                      className="min-w-0 flex-1 rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-amber-600"
                      value={stopConfirmName}
                      onChange={(e) => setStopConfirmName(e.target.value)}
                    />
                    <Button
                      variant="danger"
                      disabled={!typedConfirmationMatches(stopConfirmName, name) || busy !== null}
                      onClick={() => {
                        setStopConfirming(false)
                        setStopConfirmName('')
                        run('stop', (onProgress, onWarning) =>
                          api.stopInstance(name, undefined, onProgress, onWarning).then(assertStopSucceeded),
                        )
                      }}
                    >
                      {busy === 'stop' ? 'Stopping…' : 'Confirm stop'}
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={busy !== null}
                      onClick={() => {
                        setStopConfirming(false)
                        setStopConfirmName('')
                      }}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
              {record.manual_override_expires_at && (
                <Button
                  variant="secondary"
                  disabled={busy !== null}
                  onClick={() => run('extend', () => api.extendOverride(name))}
                >
                  {busy === 'extend' ? 'Extending…' : 'Extend override'}
                </Button>
              )}
            </div>
          </Card>

          <Card>
            <CardHeader title="Volumes" subtitle="The OS volume plus every attached data volume." />
            <ul className="divide-y divide-slate-100">
              <li className="flex items-center justify-between gap-3 px-5 py-3 text-sm">
                <div className="min-w-0">
                  <span className="font-medium text-slate-900">OS volume</span>
                  <span className="ml-2 text-xs text-slate-400">sda</span>
                </div>
                <span className="shrink-0 text-slate-500">
                  {record.os_volume_id !== null ? `#${record.os_volume_id}` : '—'}
                </span>
              </li>
              {(record.data_volumes ?? []).map((v) => (
                <li key={v.volume_id} className="flex items-center justify-between gap-3 px-5 py-3 text-sm">
                  <div className="min-w-0">
                    <span className="font-medium text-slate-900">Data volume</span>
                    <span className="ml-2 text-xs text-slate-400">{v.device_slot}</span>
                  </div>
                  <span className="shrink-0 text-slate-500">#{v.volume_id}</span>
                </li>
              ))}
            </ul>
            {(record.data_volumes ?? []).length === 0 && (
              <p className="px-5 pb-4 text-xs text-slate-500">No additional data volumes attached.</p>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Schedule"
              subtitle={
                hasActiveIndividualSchedule
                  ? 'Automatic start/stop for this instance.'
                  : currentGroup
                    ? `No individual schedule — following group "${currentGroup.name}".`
                    : 'No schedule set — manual control only.'
              }
              action={
                schedule && (
                  <Button
                    variant="ghost"
                    disabled={busy !== null}
                    onClick={() =>
                      run('clear schedule', async (_onProgress, onWarning) => {
                        const result = await api.clearSchedule(name)
                        if (result.warnings) onWarning(result.warnings)
                        return result
                      })
                    }
                  >
                    Clear
                  </Button>
                )
              }
            />
            <div className="px-5 py-4">
              <ScheduleEditor
                key={JSON.stringify(schedule)}
                initial={schedule}
                saving={busy === 'save schedule'}
                onSave={(s) =>
                  run('save schedule', async (_onProgress, onWarning) => {
                    const result = await api.setSchedule(name, s)
                    if (result.warnings) onWarning(result.warnings)
                    return result
                  })
                }
              />
            </div>
          </Card>

          <Card>
            <CardHeader title="Recent activity" subtitle="Create/delete events for this instance." />
            {history.length === 0 ? (
              <p className="px-5 py-6 text-sm text-slate-500">Nothing recorded yet.</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {history.map((event, i) => (
                  <li key={i} className="flex items-center justify-between px-5 py-3 text-sm">
                    <div>
                      <span className="font-medium text-slate-900">{event.action}</span>
                      <span className="ml-2 text-slate-400">{triggerLabel(event)}</span>
                      {event.error_message && (
                        <p className="mt-0.5 text-xs text-red-600">{event.error_message}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-3">
                      <span
                        className={`text-xs font-medium ${event.result === 'success' ? 'text-emerald-600' : 'text-red-600'}`}
                      >
                        {event.result}
                      </span>
                      <span className="text-xs text-slate-400">
                        {new Date(event.timestamp).toLocaleString()}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
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
            <CardHeader title="Group membership" />
            <div className="space-y-3 px-5 py-4">
              <select
                className="block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                value={currentGroup?.name ?? ''}
                disabled={busy !== null}
                onChange={(e) => {
                  setPendingGroupRemoval(false)
                  const groupName = e.target.value
                  if (!groupName) {
                    if (hasActiveIndividualSchedule) {
                      removeFromGroup(false)
                    } else {
                      setPendingGroupRemoval(true)
                    }
                  } else {
                    run('assign to group', async (_onProgress, onWarning) => {
                      const result = await api.patchInstanceGroup(name, { group_name: groupName })
                      if (result.warnings) onWarning(result.warnings)
                      return result
                    })
                  }
                }}
              >
                <option value="">No group</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.name}>
                    {g.name}
                  </option>
                ))}
              </select>
              {pendingGroupRemoval && currentGroup && (
                <div className="flex flex-col gap-2 rounded-md bg-amber-50 p-3 ring-1 ring-inset ring-amber-200">
                  <p className="text-xs text-amber-800">
                    “{name}” has no individual schedule of its own and is governed entirely by “
                    {currentGroup.name}”’s schedule. Removing it from the group would leave it with{' '}
                    <strong>no schedule at all</strong> unless you choose otherwise now.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="secondary"
                      className="flex-1"
                      disabled={busy !== null}
                      onClick={() => removeFromGroup(true)}
                    >
                      Copy “{currentGroup.name}”’s rules
                    </Button>
                    <Button
                      variant="secondary"
                      className="flex-1"
                      disabled={busy !== null}
                      onClick={() => removeFromGroup(false)}
                    >
                      Keep manual (no schedule)
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={busy !== null}
                      onClick={() => setPendingGroupRemoval(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
              <p className="text-xs text-slate-500">
                This instance's own schedule above always wins over its group's, if it has one. Removing from
                a group with no individual schedule set leaves it manual-only.
              </p>
            </div>
          </Card>

          <Card className="border-red-200">
            <CardHeader title="Danger zone" subtitle="Permanently stop managing this instance." />
            <div className="space-y-3 px-5 py-4">
              {offboardSuccessWarnings ? (
                <>
                  <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                    Offboarded successfully — but review this first:
                  </div>
                  <WarningBanner messages={offboardSuccessWarnings} />
                  <Button variant="primary" className="w-full" onClick={() => navigate('/instances')}>
                    Continue
                  </Button>
                </>
              ) : record.current_status !== 'stopped' && record.current_status !== 'needs_manual_recovery' ? (
                <p className="text-sm text-slate-500">Stop this instance first before offboarding it.</p>
              ) : (
                <>
                  <label className="flex items-center gap-2 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-slate-300 text-red-600 focus:ring-red-600"
                      checked={deleteVolumes}
                      onChange={(e) => setDeleteVolumes(e.target.checked)}
                    />
                    Also permanently delete its OS + data volumes
                  </label>
                  <TypeToConfirmInput
                    expected={name}
                    value={confirmName}
                    onChange={setConfirmName}
                    ringClassName="focus:ring-red-600"
                  />
                  {offboardErrorWarnings && <WarningBanner messages={offboardErrorWarnings} />}
                  {offboardError && <ErrorBanner message={offboardError} />}
                  <Button
                    variant="danger"
                    className="w-full"
                    disabled={!typedConfirmationMatches(confirmName, name) || busy !== null}
                    onClick={offboard}
                  >
                    {busy === 'offboard' ? 'Offboarding…' : 'Offboard instance'}
                  </Button>
                </>
              )}
            </div>
          </Card>

          <Card className="border-amber-200">
            <CardHeader
              title="Remove from tracking"
              subtitle="For a record that's wrong or unsafe — e.g. onboarded without proper Block Storage migration. Does not touch the instance, its volumes, its reserved IP, or its tags on Linode. Available in any status, including if it's currently unreachable."
            />
            <div className="space-y-3 px-5 py-4">
              <TypeToConfirmInput
                expected={name}
                value={deregisterConfirmName}
                onChange={setDeregisterConfirmName}
                ringClassName="focus:ring-amber-600"
              />
              {deregisterError && <ErrorBanner message={deregisterError} />}
              <Button
                variant="secondary"
                className="w-full"
                disabled={!typedConfirmationMatches(deregisterConfirmName, name) || busy !== null}
                onClick={deregister}
              >
                {busy === 'deregister' ? 'Removing…' : 'Remove from tracking'}
              </Button>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}
function triggerLabel(event: ScheduleEvent): string {
  if (event.triggered_by === 'schedule') return 'via schedule (automatic)'
  if (event.triggered_by === 'api')
    return event.actor ? `via dashboard by ${event.actor}` : 'via dashboard/API'
  return 'manually (CLI)'
}
function Info({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-0.5 text-sm text-slate-900">{value ?? '—'}</p>
    </div>
  )
}
