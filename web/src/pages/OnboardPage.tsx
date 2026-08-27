import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import type { LinodeRawInstance } from '../api/types'
import {
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  Spinner,
  TypeToConfirmInput,
  typedConfirmationMatches,
  WarningBanner,
} from '../components/ui'
import { useSshCredentials } from '../hooks/useSshCredentials'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,27}$/
type OnboardBlocker =
  | {
      kind: 'error'
      message: string
    }
  | {
      kind: 'unreserved_ip'
      address: string
    }
  | {
      kind: 'not_migrated'
      instanceId: number
    }
  | {
      kind: 'already_onboarded'
      name: string
    }
export function OnboardPage() {
  const navigate = useNavigate()
  const statusBar = useStatusBar()
  const [searchParams] = useSearchParams()
  const resumeName = searchParams.get('resumeName')
  const resumeInstanceId = searchParams.get('resumeInstanceId')
  const resumeForce = searchParams.get('resumeForce') === '1'
  const [raw, setRaw] = useState<LinodeRawInstance[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [regionFilter, setRegionFilter] = useState('')
  const [tagFilter, setTagFilter] = useState('')
  const [selected, setSelected] = useState<LinodeRawInstance | null>(null)
  const [name, setName] = useState('')
  const [onboarding, setOnboarding] = useState(false)
  const [blocker, setBlocker] = useState<OnboardBlocker | null>(null)
  const [successWarnings, setSuccessWarnings] = useState<string[] | null>(null)
  const [pendingInstanceName, setPendingInstanceName] = useState<string | null>(null)
  const [blockerWarnings, setBlockerWarnings] = useState<string[] | null>(null)
  const [reserving, setReserving] = useState(false)
  const [forceConfirmName, setForceConfirmName] = useState('')
  const ssh = useSshCredentials(selected?.ipv4[0])
  const lastForceRef = useRef(false)
  const currentAttemptKeyRef = useRef<string | null>(`${name}:${selected?.id ?? ''}`)
  useEffect(() => {
    currentAttemptKeyRef.current = `${name}:${selected?.id ?? ''}`
    return () => {
      currentAttemptKeyRef.current = null
    }
  }, [name, selected])
  useEffect(() => {
    api
      .listLinodeInstances()
      .then(setRaw)
      .catch((e) => setLoadError(e instanceof ApiError ? e.message : 'Could not load Linode instances.'))
  }, [])
  useEffect(() => {
    if (!raw || !resumeInstanceId) return
    const match = raw.find((i) => i.id === Number(resumeInstanceId))
    if (match) {
      setSelected(match)
      if (resumeName) setName(resumeName)
    }
  }, [raw, resumeInstanceId, resumeName])
  const autoForceFiredRef = useRef(false)
  useEffect(() => {
    if (!resumeForce || !selected || autoForceFiredRef.current) return
    autoForceFiredRef.current = true
    void onboard(true)
  }, [resumeForce, selected])
  const regions = useMemo(() => Array.from(new Set((raw ?? []).map((i) => i.region))).sort(), [raw])
  const tags = useMemo(() => Array.from(new Set((raw ?? []).flatMap((i) => i.tags))).sort(), [raw])
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (raw ?? []).filter((i) => {
      if (regionFilter && i.region !== regionFilter) return false
      if (tagFilter && !i.tags.includes(tagFilter)) return false
      if (q && !i.label.toLowerCase().includes(q) && !String(i.id).includes(q)) return false
      return true
    })
  }, [raw, search, regionFilter, tagFilter])
  const nameError =
    name.length > 0 && !NAME_RE.test(name)
      ? "must start with a lowercase letter/digit, contain only lowercase letters, digits, '-', '_', and be 1-28 characters"
      : null
  const onboard = async (force = false) => {
    if (!selected) return
    const requestedKey = `${name}:${selected.id}`
    lastForceRef.current = force
    setOnboarding(true)
    setBlocker((prev) => (force && prev?.kind === 'already_onboarded' ? prev : null))
    setBlockerWarnings(null)
    try {
      const result = await statusBar.run(`Onboarding “${name}”`, () =>
        api.onboardInstance({ name, instance_id: selected.id, force, ...ssh.payload }),
      )
      if (requestedKey !== currentAttemptKeyRef.current) return
      if (result.outcome === 'onboarded') {
        ssh.reset()
        lastForceRef.current = false
        setBlocker(null)
        if (result.warnings && result.warnings.length > 0) {
          setSuccessWarnings(result.warnings)
          setPendingInstanceName(name)
        } else {
          navigate(`/instances/${encodeURIComponent(name)}`)
        }
      } else {
        setBlocker({ kind: 'error', message: 'Onboarding was declined (identity change not confirmed).' })
      }
    } catch (e) {
      if (requestedKey !== currentAttemptKeyRef.current) return
      setBlockerWarnings(errorWarnings(e))
      if (e instanceof ApiError && e.status === 409 && typeof e.body?.unreserved_ip === 'string') {
        setBlocker({ kind: 'unreserved_ip', address: e.body.unreserved_ip })
      } else if (
        e instanceof ApiError &&
        e.status === 409 &&
        typeof e.body?.not_migrated_instance_id === 'number'
      ) {
        setBlocker({ kind: 'not_migrated', instanceId: e.body.not_migrated_instance_id })
      } else if (
        e instanceof ApiError &&
        e.status === 409 &&
        typeof e.body?.already_onboarded_name === 'string'
      ) {
        setBlocker({ kind: 'already_onboarded', name: e.body.already_onboarded_name })
      } else {
        const message = e instanceof ApiError ? e.message : 'Could not onboard this instance.'
        setBlocker({ kind: 'error', message })
        ssh.openIfLikelySshFailure(message)
      }
    } finally {
      if (requestedKey === currentAttemptKeyRef.current) setOnboarding(false)
    }
  }
  const reserveIpAndRetry = async () => {
    if (blocker?.kind !== 'unreserved_ip') return
    const address = blocker.address
    const requestedKey = currentAttemptKeyRef.current
    setReserving(true)
    try {
      await statusBar.run(`Reserving ${address}`, () => api.reserveIp(address))
      await onboard(lastForceRef.current)
    } catch (e) {
      if (requestedKey !== currentAttemptKeyRef.current) return
      setBlockerWarnings(errorWarnings(e))
      setBlocker({
        kind: 'error',
        message: e instanceof ApiError ? e.message : 'Could not reserve that IP.',
      })
    } finally {
      if (requestedKey === currentAttemptKeyRef.current) setReserving(false)
    }
  }
  return (
    <div>
      <PageHeader
        title="Onboard an instance"
        subtitle="Bring an existing, running Linode instance under this tool's management."
      />
      <div className="grid grid-cols-3 gap-6 p-8">
        <div className="col-span-2 space-y-4">
          <div className="flex flex-wrap gap-3">
            <input
              className="min-w-[220px] flex-1 rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
              placeholder="Search by label or ID…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <select
              className="rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
              value={regionFilter}
              onChange={(e) => setRegionFilter(e.target.value)}
            >
              <option value="">All regions</option>
              {regions.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <select
              className="rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
              value={tagFilter}
              onChange={(e) => setTagFilter(e.target.value)}
            >
              <option value="">All tags</option>
              {tags.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>

          {loadError && <ErrorBanner message={loadError} />}
          {!loadError && raw === null && (
            <div className="flex justify-center py-16">
              <Spinner className="text-indigo-600" />
            </div>
          )}
          {raw && filtered.length === 0 && (
            <Card>
              <EmptyState title="No matching Linode instances" subtitle="Try a different search or filter." />
            </Card>
          )}
          {raw && filtered.length > 0 && (
            <Card className="max-h-[32rem] divide-y divide-slate-100 overflow-y-auto">
              {filtered.map((inst) => {
                const disabled = inst.onboarded_as !== null
                const isSelected = selected?.id === inst.id
                return (
                  <button
                    key={inst.id}
                    type="button"
                    disabled={disabled}
                    onClick={() => {
                      setSelected(inst)
                      if (blocker?.kind !== 'already_onboarded') {
                        setBlocker(null)
                        setBlockerWarnings(null)
                        lastForceRef.current = false
                      }
                      setSuccessWarnings(null)
                      setPendingInstanceName(null)
                      ssh.reset()
                    }}
                    className={`flex w-full items-center justify-between px-5 py-3 text-left text-sm transition disabled:cursor-not-allowed disabled:opacity-50 ${isSelected ? 'bg-indigo-50' : 'hover:bg-slate-50'}`}
                  >
                    <div>
                      <p className="font-medium text-slate-900">{inst.label}</p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        #{inst.id} · {inst.region} · {inst.status}
                        {inst.tags.length > 0 && ` · ${inst.tags.join(', ')}`}
                      </p>
                    </div>
                    {disabled ? (
                      <span className="text-xs font-medium text-slate-400">
                        already onboarded as “{inst.onboarded_as}”
                      </span>
                    ) : (
                      isSelected && <span className="text-xs font-semibold text-indigo-600">Selected</span>
                    )}
                  </button>
                )
              })}
            </Card>
          )}
        </div>

        <div>
          <Card>
            <div className="space-y-4 px-5 py-5">
              <h2 className="text-sm font-semibold text-slate-900">Onboard</h2>
              {successWarnings ? (
                <>
                  <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                    Onboarded successfully — but review this first:
                  </div>
                  <WarningBanner messages={successWarnings} />
                  <Button
                    variant="primary"
                    className="w-full"
                    onClick={() => navigate(`/instances/${encodeURIComponent(pendingInstanceName ?? name)}`)}
                  >
                    Continue to instance
                  </Button>
                </>
              ) : !selected ? (
                <p className="text-sm text-slate-500">Select an instance from the list to continue.</p>
              ) : (
                <>
                  <div className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
                    <p className="font-medium text-slate-900">{selected.label}</p>
                    <p>
                      #{selected.id} · {selected.region}
                    </p>
                  </div>
                  <label className="block">
                    <span className="text-sm font-medium text-slate-700">Name in this tool</span>
                    <input
                      className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                      value={name}
                      onChange={(e) => {
                        const next = e.target.value.toLowerCase()
                        setName(next)
                        if (blocker?.kind === 'already_onboarded') {
                          setBlocker(null)
                          setBlockerWarnings(null)
                        }
                        setForceConfirmName('')
                        lastForceRef.current = false
                      }}
                      placeholder="redis-standby-1"
                    />
                    {nameError && <p className="mt-1 text-xs text-red-600">{nameError}</p>}
                  </label>
                  {selected.status !== 'running' && (
                    <p className="text-xs text-amber-600">
                      This instance is currently “{selected.status}” — onboarding requires it to be running
                      (network config and data volumes are captured live, over SSH).
                    </p>
                  )}
                  {blockerWarnings && <WarningBanner messages={blockerWarnings} />}
                  {blocker?.kind === 'error' && <ErrorBanner message={blocker.message} />}
                  {blocker?.kind === 'unreserved_ip' && (
                    <div className="rounded-md bg-amber-50 px-3 py-3 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
                      <p>
                        <strong>{blocker.address}</strong> isn't reserved yet — if this tool ever stops the
                        instance, that address would return to Linode's pool and could be lost or reassigned
                        to someone else.
                      </p>
                      <p className="mt-1">
                        Reserving it is a real, permanent account change (and may carry a small idle cost if
                        the address is later unattached).
                      </p>
                      <Button
                        variant="secondary"
                        className="mt-2 w-full"
                        disabled={reserving}
                        onClick={reserveIpAndRetry}
                      >
                        {reserving ? 'Reserving…' : `Reserve ${blocker.address} and continue onboarding`}
                      </Button>
                    </div>
                  )}
                  {blocker?.kind === 'not_migrated' && (
                    <div className="rounded-md bg-amber-50 px-3 py-3 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
                      <p>
                        This instance's OS is still on <strong>local disk</strong>, not a Block Storage volume
                        — onboarding needs that done first, since a stop/start cycle deletes and recreates the
                        instance, and local disk doesn't survive that the way a volume does.
                      </p>
                      <p className="mt-1">
                        This is a one-time migration with one manual step (a real data copy you run yourself
                        in Lish) — we'll walk you through it.
                      </p>
                      <Button
                        variant="secondary"
                        className="mt-2 w-full"
                        onClick={() =>
                          navigate(
                            `/migrate?name=${encodeURIComponent(name)}&instanceId=${blocker.instanceId}` +
                              (lastForceRef.current ? '&force=1' : ''),
                          )
                        }
                      >
                        Migrate this instance onto Block Storage
                      </Button>
                    </div>
                  )}
                  {blocker?.kind === 'already_onboarded' && (
                    <div className="rounded-md bg-amber-50 px-3 py-3 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
                      <p>
                        A record already exists for “{blocker.name}”. Forcing re-onboard overwrites it in
                        place with a fresh live capture from this instance — the way to recover a{' '}
                        <code>needs_manual_recovery</code> node once a real instance is standing in for it
                        again.
                      </p>
                      <div className="mt-2">
                        <TypeToConfirmInput
                          expected={blocker.name}
                          value={forceConfirmName}
                          onChange={setForceConfirmName}
                          autoFocus
                          ringClassName="focus:ring-amber-600"
                          label={<span className="text-amber-700">Type “{blocker.name}” to confirm</span>}
                        />
                      </div>
                      <Button
                        variant="danger"
                        className="mt-2 w-full"
                        disabled={!typedConfirmationMatches(forceConfirmName, blocker.name) || onboarding}
                        onClick={() => {
                          setForceConfirmName('')
                          onboard(true)
                        }}
                      >
                        {onboarding ? 'Onboarding…' : `Force re-onboard "${blocker.name}"`}
                      </Button>
                    </div>
                  )}
                  {ssh.node}
                  <Button
                    variant="primary"
                    className="w-full"
                    disabled={!name || !!nameError || onboarding || reserving || !ssh.isValid}
                    onClick={() => onboard(lastForceRef.current)}
                  >
                    {onboarding ? 'Onboarding…' : 'Onboard instance'}
                  </Button>
                </>
              )}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
