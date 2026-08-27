import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import {
  Button,
  Card,
  ErrorBanner,
  ProgressBar,
  Spinner,
  TypeToConfirmInput,
  typedConfirmationMatches,
  WarningBanner,
} from '../components/ui'
import { useSshCredentials } from '../hooks/useSshCredentials'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
type Step =
  | 'loading'
  | 'start'
  | 'starting'
  | 'manual'
  | 'resuming'
  | 'onboarding'
  | 'done'
  | 'error'
  | 'incomplete'
  | 'setup_incomplete'
  | 'live'
  | 'reconciled'
const DD_HINT = 'dd if=/dev/sda of=/dev/sdb bs=4M status=progress && sync'
export function MigratePage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const statusBar = useStatusBar()
  const name = searchParams.get('name') ?? ''
  const instanceIdParam = searchParams.get('instanceId')
  const instanceId = instanceIdParam ? Number(instanceIdParam) : null
  const force = searchParams.get('force') === '1'
  const migrationKey = `migrate:${name}:${instanceId ?? ''}`
  const returnToThisMigration = () =>
    navigate(
      `/migrate?name=${encodeURIComponent(name)}&instanceId=${instanceId ?? ''}` + (force ? '&force=1' : ''),
    )
  const [step, setStep] = useState<Step>('loading')
  const [error, setError] = useState<string | null>(null)
  const [migrateWarnings, setMigrateWarnings] = useState<string[]>([])
  const [onboardSuccessWarnings, setOnboardSuccessWarnings] = useState<string[] | null>(null)
  const [progress, setProgress] = useState<{
    percent: number
    label: string | null
  } | null>(null)
  const [resolvedInstanceId, setResolvedInstanceId] = useState<number | null>(instanceId)
  const [destVolumeSizeGb, setDestVolumeSizeGb] = useState<number | null>(null)
  const [localDiskSizeMb, setLocalDiskSizeMb] = useState<number | null>(null)
  const [pastedOutput, setPastedOutput] = useState('')
  const [confirmName, setConfirmName] = useState('')
  const [onboardError, setOnboardError] = useState<string | null>(null)
  const [onboardErrorWarnings, setOnboardErrorWarnings] = useState<string[] | null>(null)
  const ssh = useSshCredentials(undefined)
  const currentKeyRef = useRef<string | null>(migrationKey)
  useEffect(() => {
    currentKeyRef.current = migrationKey
    return () => {
      currentKeyRef.current = null
    }
  }, [migrationKey])
  const checkResumability = () => {
    const requestedKey = migrationKey
    api
      .getMigrateStatus(name)
      .then((status) => {
        if (requestedKey !== currentKeyRef.current) return
        if (status.in_progress && status.phase === 'awaiting_manual_dd') {
          setResolvedInstanceId(status.instance_id ?? instanceId)
          setDestVolumeSizeGb(status.dest_volume_size_gb ?? null)
          setLocalDiskSizeMb(status.local_disk_size_mb ?? null)
          setStep('manual')
        } else if (status.in_progress) {
          setStep('setup_incomplete')
        } else if (instanceId === null) {
          setStep('error')
          setError('No instance selected -- go back to Onboard and try again.')
        } else {
          setStep('start')
        }
      })
      .catch((e) => {
        if (requestedKey !== currentKeyRef.current) return
        setStep('error')
        setError(e instanceof ApiError ? e.message : 'Could not check migration status.')
      })
  }
  useEffect(() => {
    setStep('loading')
    ssh.reset()
    setResolvedInstanceId(instanceId)
    setDestVolumeSizeGb(null)
    setLocalDiskSizeMb(null)
    setPastedOutput('')
    setConfirmName('')
    setOnboardError(null)
    setOnboardErrorWarnings(null)
    setMigrateWarnings([])
    setOnboardSuccessWarnings(null)
    if (!name) {
      setStep('error')
      setError('No instance name given -- go back to Onboard and try again.')
      return
    }
    if (statusBar.current?.key === migrationKey) {
      setStep('live')
      return
    }
    checkResumability()
  }, [name, instanceId])
  useEffect(() => {
    if (step !== 'live') return
    const current = statusBar.current
    if (current?.key !== migrationKey || current.kind === 'pending') {
      if (current?.key === migrationKey) return
      checkResumability()
      return
    }
    if (current.kind === 'error') {
      if (current.phase === 'finish') {
        setOnboardError(current.detail ?? 'Could not finish onboarding automatically.')
        setOnboardErrorWarnings(null)
        setStep('done')
        return
      }
      setError(current.detail ?? 'Migration failed.')
      if (current.phase === 'resume') {
        checkResumability()
      } else {
        setStep('start')
      }
      return
    }
    if (current.phase === 'start') {
      checkResumability()
      return
    }
    const requestedKey = migrationKey
    api.getInstance(name).then(
      () => {
        if (requestedKey === currentKeyRef.current) setStep('reconciled')
      },
      () => checkResumability(),
    )
  }, [step, statusBar.current])
  const startMigration = async () => {
    if (instanceId === null) return
    const requestedKey = migrationKey
    setStep('starting')
    setError(null)
    setMigrateWarnings([])
    setProgress({ percent: 0, label: null })
    try {
      const result = await statusBar.run(
        `Migrating "${name}" to Block Storage`,
        (report) =>
          api.migrateStart(
            name,
            instanceId,
            ssh.payload,
            (percent, currentStep) => {
              if (requestedKey === currentKeyRef.current) {
                setProgress({ percent, label: currentStep })
              }
              report(percent, currentStep)
            },
            (warnings) => {
              if (requestedKey === currentKeyRef.current) setMigrateWarnings(warnings)
            },
          ),
        { onNavigate: returnToThisMigration, key: migrationKey, phase: 'start' },
      )
      if (requestedKey !== currentKeyRef.current) return
      ssh.reset()
      setResolvedInstanceId(result.instance_id ?? instanceId)
      setDestVolumeSizeGb(result.dest_volume_size_gb)
      setLocalDiskSizeMb(result.local_disk_size_mb)
      setStep('manual')
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      const carried = errorWarnings(e)
      if (carried) setMigrateWarnings((prev) => [...prev, ...carried])
      const message = e instanceof ApiError ? e.message : 'Could not start migration.'
      setError(message)
      ssh.openIfLikelySshFailure(message)
      setConfirmName('')
      setStep('start')
    } finally {
      if (requestedKey === currentKeyRef.current) setProgress(null)
    }
  }
  const resumeMigration = async () => {
    const requestedKey = migrationKey
    setStep('resuming')
    setError(null)
    setMigrateWarnings([])
    setProgress({ percent: 0, label: null })
    try {
      const result = await statusBar.run(
        `Finishing migration for "${name}"`,
        (report) =>
          api.migrateResume(
            name,
            (percent, currentStep) => {
              if (requestedKey === currentKeyRef.current) {
                setProgress({ percent, label: currentStep })
              }
              report(percent, currentStep)
            },
            (warnings) => {
              if (requestedKey === currentKeyRef.current) {
                setMigrateWarnings((prev) => [...prev, ...warnings])
              }
            },
          ),
        { onNavigate: returnToThisMigration, key: migrationKey, phase: 'resume' },
      )
      if (requestedKey !== currentKeyRef.current) return
      if (result.outcome === 'resumed') {
        const resumeNotes: string[] = []
        if (result.previous_attempts_count > 0) {
          resumeNotes.push(
            `${result.previous_attempts_count} earlier incomplete migration attempt(s) for ` +
              `"${name}" were replaced before this one. Their resource IDs (most importantly ` +
              'any destination volume) are preserved and were NOT deleted automatically -- ' +
              `they are likely still billing. Run \`migrate-orphans --name ${name}\` (CLI) to ` +
              "see them, and `--cleanup` to delete them once you're sure they're no longer needed.",
          )
        }
        if (result.fstab_entries_disabled.length > 0) {
          resumeNotes.push(
            `${result.fstab_entries_disabled.length} stale /etc/fstab entr` +
              `${result.fstab_entries_disabled.length === 1 ? 'y was' : 'ies were'} disabled ` +
              '(device no longer present after migration -- would otherwise stall every future ' +
              'boot). A backup was made before editing.',
          )
        }
        if (resumeNotes.length > 0) setMigrateWarnings((prev) => [...prev, ...resumeNotes])
        await finishOnboarding(resumeNotes)
      } else {
        setError(
          result.detail ??
            'The migration itself completed, but recording that locally failed. Check the CLI (`migrate-resume`/`onboard`) to finish.',
        )
        setStep('incomplete')
      }
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      const carried = errorWarnings(e)
      if (carried) setMigrateWarnings((prev) => [...prev, ...carried])
      setError(e instanceof ApiError ? e.message : 'Could not resume migration.')
      setStep('manual')
    } finally {
      if (requestedKey === currentKeyRef.current) setProgress(null)
    }
  }
  const finishOnboarding = async (migrateResumeNotes: string[] = []) => {
    const requestedKey = migrationKey
    const instId = resolvedInstanceId ?? instanceId
    if (instId === null) {
      setStep('done')
      return
    }
    setStep('onboarding')
    setOnboardError(null)
    setOnboardErrorWarnings(null)
    try {
      const result = await statusBar.run(
        `Finishing onboarding for "${name}"`,
        () => api.onboardInstance({ name, instance_id: instId, force }),
        { onNavigate: returnToThisMigration, key: migrationKey, phase: 'finish' },
      )
      if (requestedKey !== currentKeyRef.current) return
      if (result.outcome === 'onboarded') {
        if ((result.warnings && result.warnings.length > 0) || migrateResumeNotes.length > 0) {
          setOnboardSuccessWarnings(result.warnings ?? [])
          setStep('done')
          return
        }
        navigate(`/instances/${encodeURIComponent(name)}`)
        return
      }
      setOnboardError('Onboarding was declined (identity change not confirmed).')
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      setOnboardErrorWarnings(errorWarnings(e))
      setOnboardError(e instanceof ApiError ? e.message : 'Could not finish onboarding automatically.')
    }
    if (requestedKey === currentKeyRef.current) setStep('done')
  }
  return (
    <div>
      <PageHeader title="Migrate onto Block Storage" subtitle={name ? `For “${name}”` : undefined} />
      <div className="mx-auto max-w-2xl p-8">
        <Card>
          <div className="space-y-5 px-6 py-6">
            {step === 'loading' && (
              <div className="flex justify-center py-8">
                <Spinner className="text-indigo-600" />
              </div>
            )}

            {step === 'error' && (
              <>
                {error && <ErrorBanner message={error} />}
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
              </>
            )}

            {step === 'live' && (
              <>
                <p className="text-sm text-slate-700">
                  A migration for “{name}” is already in progress — reconnected to its real, live status
                  below.
                </p>
                {statusBar.current?.key === migrationKey && typeof statusBar.current.percent === 'number' && (
                  <ProgressBar percent={statusBar.current.percent} label={statusBar.current.detail ?? null} />
                )}
              </>
            )}

            {step === 'reconciled' && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  Migration and onboarding for “{name}” completed successfully while this page wasn't open —
                  some details from that run (e.g. an identity-change notice from a force re-onboard) may not
                  be shown here. Check the instance's own page for its current status.
                </div>
                <Button
                  variant="primary"
                  className="w-full"
                  onClick={() => navigate(`/instances/${encodeURIComponent(name)}`)}
                >
                  Continue to instance
                </Button>
              </>
            )}

            {step === 'start' && (
              <>
                <div className="space-y-2 text-sm text-slate-700">
                  <p>
                    This instance's OS is currently on <strong>local disk</strong>, not a Block Storage
                    volume. This tool works by deleting and recreating the instance on every stop/start cycle
                    — local disk doesn't survive that, a Block Storage volume does.
                  </p>
                  <p>
                    This is a <strong>one-time migration</strong>. Almost everything is automated; there's
                    exactly one manual step (copying the disk yourself, in a Lish console) that genuinely
                    can't be done through the API. We'll walk you through it.
                  </p>
                </div>

                <div className="rounded-md bg-red-50 px-3 py-3 text-sm text-red-800 ring-1 ring-inset ring-red-200">
                  <p className="font-medium">This causes real downtime.</p>
                  <p className="mt-1">
                    In a moment, this instance reboots into Rescue Mode and is{' '}
                    <strong>completely unavailable</strong> for the entire duration of the manual copy step
                    below — which can take anywhere from a few minutes to well over an hour depending on how
                    much data is on it — and again briefly when it reboots back afterward. Plan this for a
                    scheduled maintenance window or off-hours, not while this instance is serving live
                    traffic.
                  </p>
                </div>

                {error && <ErrorBanner message={error} />}
                {ssh.node}

                <TypeToConfirmInput
                  expected={name}
                  value={confirmName}
                  onChange={setConfirmName}
                  ringClassName="focus:ring-red-600"
                  label={<>Type “{name}” to confirm you understand this instance will go down now</>}
                />

                <Button
                  variant="danger"
                  className="w-full"
                  disabled={!ssh.isValid || !typedConfirmationMatches(confirmName, name)}
                  onClick={startMigration}
                >
                  Start migration now
                </Button>
              </>
            )}

            {step === 'starting' && (
              <>
                <p className="text-sm text-slate-700">
                  Creating the destination volume and preparing Rescue Mode — this involves a few real API
                  calls and can take a little while.
                </p>
                {progress && <ProgressBar percent={progress.percent} label={progress.label} />}
              </>
            )}

            {(step === 'manual' || step === 'resuming') && (
              <>
                {migrateWarnings.length > 0 && <WarningBanner messages={migrateWarnings} />}
                {(destVolumeSizeGb !== null || localDiskSizeMb !== null) && (
                  <div className="rounded-md bg-slate-50 px-3 py-2.5 text-xs text-slate-600">
                    <p>
                      Source (local disk): <strong>~{localDiskSizeMb ?? '?'} MB</strong>
                    </p>
                    <p>
                      Destination (new Block Storage volume): <strong>~{destVolumeSizeGb ?? '?'} GB</strong>
                    </p>
                    <p className="mt-1.5">
                      This copies the full disk, not just used space — depending on your account's disk
                      throughput, that can take anywhere from a few minutes to well over an hour for a larger
                      disk. Please be patient and let it finish cleanly rather than interrupting it.
                    </p>
                  </div>
                )}

                <div className="space-y-3 text-sm text-slate-700">
                  <p className="font-medium text-slate-900">Your turn — the one manual step:</p>
                  <ol className="list-decimal space-y-2 pl-5">
                    <li>
                      In Cloud Manager, open instance <strong>{resolvedInstanceId ?? instanceId}</strong> and
                      click "Launch LISH Console."
                    </li>
                    <li>Log in as root.</li>
                    <li>
                      Run <code className="rounded bg-slate-100 px-1 py-0.5">lsblk</code> FIRST and match
                      devices by the sizes above — <strong>do not trust /dev/sda/sdb blindly.</strong> Rescue
                      Mode's own device assignment isn't guaranteed to match what was requested. If lsblk
                      shows different devices, substitute the correct names below.
                    </li>
                    <li>
                      Once confirmed, run:
                      <div className="mt-1.5 flex items-center gap-2">
                        <code className="block flex-1 overflow-x-auto rounded-md bg-slate-900 px-3 py-2 text-xs text-slate-100">
                          {DD_HINT}
                        </code>
                        <Button
                          variant="secondary"
                          className="!px-2.5 !py-1.5 text-xs"
                          onClick={() => navigator.clipboard?.writeText(DD_HINT)}
                        >
                          Copy
                        </Button>
                      </div>
                    </li>
                    <li>Wait for it to finish cleanly, with no I/O errors.</li>
                  </ol>
                </div>

                <label className="block">
                  <span className="text-xs font-medium text-slate-500">
                    Paste the terminal output here once it finishes (a sanity check for you — the real
                    verification happens automatically in the next step, over SSH)
                  </span>
                  <textarea
                    className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 font-mono text-xs text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                    rows={4}
                    placeholder="12345678901+0 records in&#10;12345678901+0 records out&#10;...&#10;$"
                    value={pastedOutput}
                    onChange={(e) => setPastedOutput(e.target.value)}
                    disabled={step === 'resuming'}
                  />
                </label>

                {error && <ErrorBanner message={error} />}

                {step === 'resuming' ? (
                  <>
                    <p className="text-sm text-slate-700">
                      Exiting Rescue Mode, booting the real config, and verifying the migrated volume is
                      genuinely serving root...
                    </p>
                    {progress && <ProgressBar percent={progress.percent} label={progress.label} />}
                  </>
                ) : (
                  <Button
                    variant="primary"
                    className="w-full"
                    disabled={!pastedOutput.trim()}
                    onClick={resumeMigration}
                  >
                    I've completed the copy — finish migration
                  </Button>
                )}
              </>
            )}

            {step === 'incomplete' && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  The migration itself completed successfully — the disk copy is done and the instance
                  verified booting from the new Block Storage volume.
                </div>
                {error && <ErrorBanner message={error} />}
                <p className="text-sm text-slate-700">
                  Only recording that locally failed, which this page can't retry safely (re-running the dd
                  copy would be unnecessary and could make things worse). Use the CLI instead:
                </p>
                <code className="block overflow-x-auto rounded-md bg-slate-900 px-3 py-2 text-xs text-slate-100">
                  migrate-resume --name {name}
                </code>
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
              </>
            )}

            {step === 'setup_incomplete' && (
              <>
                <ErrorBanner message="This migration's setup was interrupted before reaching the manual copy step." />
                <p className="text-sm text-slate-700">
                  The destination volume and Rescue Mode request may not be ready yet — starting a manual data
                  copy now could target the wrong device or fail outright. This can't be safely resumed from
                  the dashboard; use the CLI's force-restart instead (creates a fresh destination volume and
                  retags the old one):
                </p>
                <code className="block overflow-x-auto rounded-md bg-slate-900 px-3 py-2 text-xs text-slate-100">
                  migrate-start --instance-id {resolvedInstanceId ?? instanceId} --force
                </code>
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
              </>
            )}

            {step === 'onboarding' && (
              <>
                <p className="text-sm text-slate-700">
                  Migration verified — finishing onboarding automatically...
                </p>
                <div className="flex justify-center py-4">
                  <Spinner className="text-indigo-600" />
                </div>
              </>
            )}

            {step === 'done' && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  Migration complete — this instance is now running from a Block Storage volume.
                </div>
                {migrateWarnings.length > 0 && <WarningBanner messages={migrateWarnings} />}
                {onboardSuccessWarnings ? (
                  <>
                    <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                      Onboarded successfully — but review this first:
                    </div>

                    {onboardSuccessWarnings.length > 0 && <WarningBanner messages={onboardSuccessWarnings} />}
                    <Button
                      variant="primary"
                      className="w-full"
                      onClick={() => navigate(`/instances/${encodeURIComponent(name)}`)}
                    >
                      Continue to instance
                    </Button>
                  </>
                ) : (
                  <>
                    {onboardErrorWarnings && <WarningBanner messages={onboardErrorWarnings} />}
                    {onboardError && <ErrorBanner message={`Automatic onboarding failed: ${onboardError}`} />}
                    <Button
                      variant="primary"
                      className="w-full"
                      onClick={() =>
                        navigate(
                          `/onboard?resumeName=${encodeURIComponent(name)}&resumeInstanceId=${resolvedInstanceId ?? instanceId ?? ''}` +
                            (force ? '&resumeForce=1' : ''),
                        )
                      }
                    >
                      {onboardError ? 'Finish onboarding manually' : 'Continue to onboarding'}
                    </Button>
                  </>
                )}
              </>
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}
