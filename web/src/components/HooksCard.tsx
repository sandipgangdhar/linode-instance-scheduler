import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { EffectiveHooks, HookConfig, HookEvent, HookFailurePolicy, HookRunResult } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, Spinner, WarningBanner } from './ui'
const DEFAULT_PRE_STOP_TIMEOUT_S = 300
const DEFAULT_POST_START_TIMEOUT_S = 600
const MAX_TIMEOUT_S = 3600
const MAX_SCRIPT_BYTES = 1024 * 1024
type HookMode = 'command' | 'script'
export type HooksTarget =
  | {
      kind: 'instance'
      name: string
    }
  | {
      kind: 'group'
      name: string
    }
const inputClass =
  'mt-1 block w-full rounded-md border-0 py-1.5 px-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
export function HooksCard({
  target,
  instanceRunning = false,
  refreshToken,
}: {
  target: HooksTarget
  instanceRunning?: boolean
  refreshToken?: string
}) {
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  const [loaded, setLoaded] = useState(false)
  const [own, setOwn] = useState<HookConfig | null>(null)
  const [effective, setEffective] = useState<EffectiveHooks | null>(null)
  const [lastFailure, setLastFailure] = useState<HookEvent | null>(null)
  const [events, setEvents] = useState<HookEvent[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [preStopMode, setPreStopMode] = useState<HookMode>('command')
  const [preStopScript, setPreStopScript] = useState('')
  const [postStartMode, setPostStartMode] = useState<HookMode>('command')
  const [postStartScript, setPostStartScript] = useState('')
  const [saveWarnings, setSaveWarnings] = useState<string[] | null>(null)
  const [preStopCommand, setPreStopCommand] = useState('')
  const [preStopTimeout, setPreStopTimeout] = useState(String(DEFAULT_PRE_STOP_TIMEOUT_S))
  const [preStopOnFailure, setPreStopOnFailure] = useState<HookFailurePolicy>('abort')
  const [postStartCommand, setPostStartCommand] = useState('')
  const [postStartTimeout, setPostStartTimeout] = useState(String(DEFAULT_POST_START_TIMEOUT_S))
  const [busy, setBusy] = useState<null | 'save' | 'clear' | 'run'>(null)
  const busyRef = useRef(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [runResult, setRunResult] = useState<HookRunResult | null>(null)
  const seedForm = (config: HookConfig | null) => {
    setPreStopMode(config?.pre_stop?.script != null ? 'script' : 'command')
    setPreStopScript(config?.pre_stop?.script ?? '')
    setPostStartMode(config?.post_start?.script != null ? 'script' : 'command')
    setPostStartScript(config?.post_start?.script ?? '')
    setPreStopCommand(config?.pre_stop?.command ?? '')
    setPreStopTimeout(String(config?.pre_stop?.timeout_s ?? DEFAULT_PRE_STOP_TIMEOUT_S))
    setPreStopOnFailure(config?.pre_stop?.on_failure ?? 'abort')
    setPostStartCommand(config?.post_start?.command ?? '')
    setPostStartTimeout(String(config?.post_start?.timeout_s ?? DEFAULT_POST_START_TIMEOUT_S))
  }
  const load = useCallback(async () => {
    try {
      if (target.kind === 'instance') {
        const [hooks, hookEvents] = await Promise.all([
          api.getInstanceHooks(target.name),
          api.getHookEvents(target.name, 10).catch(() => [] as HookEvent[]),
        ])
        if (!mountedRef.current) return
        setOwn(hooks.own)
        setEffective(hooks.effective)
        setLastFailure(hooks.last_post_start_failure)
        setEvents(hookEvents)
        return hooks.own
      }
      const { hooks } = await api.getGroupHooks(target.name)
      if (!mountedRef.current) return
      setOwn(hooks)
      return hooks
    } catch (e) {
      if (mountedRef.current) setLoadError(e instanceof ApiError ? e.message : 'Could not load hooks.')
      return undefined
    }
  }, [target.kind, target.name])
  useEffect(() => {
    void load().then((config) => {
      if (!mountedRef.current) return
      if (config !== undefined) seedForm(config)
      setLoaded(true)
    })
  }, [])
  useEffect(() => {
    if (refreshToken !== undefined && loaded) void load()
  }, [refreshToken])
  const parseTimeout = (raw: string, label: string): number => {
    const n = Number(raw)
    if (!Number.isInteger(n) || n < 1 || n > MAX_TIMEOUT_S) {
      throw new Error(`${label} timeout must be a whole number of seconds from 1 to ${MAX_TIMEOUT_S}.`)
    }
    return n
  }
  const what = (
    mode: HookMode,
    command: string,
    script: string,
  ):
    | {
        command: string
      }
    | {
        script: string
      }
    | null => {
    if (mode === 'script') {
      if (!script.trim()) return null
      if (new Blob([script]).size > MAX_SCRIPT_BYTES) throw new Error('A hook script can be at most 1 MB.')
      return { script }
    }
    return command.trim() ? { command } : null
  }
  const buildConfig = (): HookConfig => {
    const pre = what(preStopMode, preStopCommand, preStopScript)
    const post = what(postStartMode, postStartCommand, postStartScript)
    return {
      pre_stop: pre
        ? { ...pre, timeout_s: parseTimeout(preStopTimeout, 'Pre-stop'), on_failure: preStopOnFailure }
        : null,
      post_start: post ? { ...post, timeout_s: parseTimeout(postStartTimeout, 'Post-start') } : null,
    }
  }
  async function act(kind: 'save' | 'clear' | 'run', fn: () => Promise<void>) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(kind)
    setActionError(null)
    try {
      await fn()
    } catch (e) {
      if (mountedRef.current) {
        setActionError(e instanceof ApiError || e instanceof Error ? e.message : 'Something went wrong.')
      }
    } finally {
      busyRef.current = false
      if (mountedRef.current) setBusy(null)
    }
  }
  const save = () =>
    act('save', async () => {
      setSaveWarnings(null)
      const config = buildConfig()
      const result =
        target.kind === 'instance'
          ? await api.setInstanceHooks(target.name, config)
          : await api.setGroupHooks(target.name, config)
      if (mountedRef.current && result.warnings?.length) setSaveWarnings(result.warnings)
      const fresh = await load()
      if (mountedRef.current && fresh !== undefined) seedForm(fresh)
    })
  const clear = () =>
    act('clear', async () => {
      if (target.kind === 'instance') await api.clearInstanceHooks(target.name)
      else await api.clearGroupHooks(target.name)
      const fresh = await load()
      if (mountedRef.current && fresh !== undefined) seedForm(fresh)
    })
  const runCheck = () =>
    act('run', async () => {
      if (target.kind !== 'instance') return
      setRunResult(null)
      const result = await api.runInstanceHook(target.name, 'post_start')
      if (!mountedRef.current) return
      setRunResult(result)
      await load()
    })
  const inheritedLabel = (hookType: 'pre_stop' | 'post_start'): string | null => {
    const hook = effective?.[hookType]
    if (!hook || hook.source !== 'group') return null
    return hook.script != null ? 'an uploaded script' : (hook.command ?? '')
  }
  const hasOwn = own !== null && (own.pre_stop !== null || own.post_start !== null)
  const effectivePostStart = effective?.post_start ?? null
  return (
    <Card>
      <CardHeader
        title="Hooks"
        subtitle={
          target.kind === 'instance'
            ? 'Commands run on this instance right before every stop and right after every start.'
            : 'Commands run on every member right before each stop and after each start, unless the member sets its own.'
        }
        action={
          hasOwn && (
            <Button variant="ghost" disabled={busy !== null} onClick={clear}>
              {busy === 'clear' ? <Spinner /> : 'Clear'}
            </Button>
          )
        }
      />
      {!loaded ? (
        <div className="px-5 py-6">
          <Spinner />
        </div>
      ) : loadError ? (
        <div className="px-5 py-4">
          <ErrorBanner message={loadError} />
        </div>
      ) : (
        <div className="space-y-5 px-5 py-4">
          <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Hooks run as <span className="font-mono">root</span> on the instance over SSH. Every change is
            recorded in the instance's hook history.
          </p>

          {lastFailure && instanceRunning && (
            <ErrorBanner
              message={`Last post-start check failed at ${new Date(lastFailure.timestamp).toLocaleString()}: ${lastFailure.detail ?? ''}`}
            />
          )}

          <fieldset className="space-y-2" data-testid="pre-stop-hook">
            <legend className="text-sm font-medium text-slate-900">Pre-stop hook</legend>
            <p className="text-xs text-slate-500">
              Runs once, right before the instance is shut down and deleted (e.g. stop a database cleanly).
            </p>
            <HookWhat
              label="Pre-stop"
              mode={preStopMode}
              onMode={setPreStopMode}
              command={preStopCommand}
              onCommand={setPreStopCommand}
              script={preStopScript}
              onScript={setPreStopScript}
              inherited={inheritedLabel('pre_stop')}
              example="e.g. pg_ctlcluster 16 main stop -m fast, or /opt/app/hooks/before-stop.sh"
            />
            <div className="flex flex-wrap gap-4">
              <label className="block text-xs font-medium text-slate-500">
                Timeout (seconds)
                <input
                  aria-label="Pre-stop timeout"
                  type="number"
                  min={1}
                  max={MAX_TIMEOUT_S}
                  className={`${inputClass} w-28`}
                  value={preStopTimeout}
                  onChange={(e) => setPreStopTimeout(e.target.value)}
                />
              </label>
              <label className="block text-xs font-medium text-slate-500">
                If it fails
                <select
                  aria-label="Pre-stop failure policy"
                  className={`${inputClass} w-64`}
                  value={preStopOnFailure}
                  onChange={(e) => setPreStopOnFailure(e.target.value as HookFailurePolicy)}
                >
                  <option value="abort">Abort the stop (keep running)</option>
                  <option value="continue">Stop anyway</option>
                </select>
              </label>
            </div>
          </fieldset>

          <fieldset className="space-y-2" data-testid="post-start-hook">
            <legend className="text-sm font-medium text-slate-900">Post-start check</legend>
            <p className="text-xs text-slate-500">
              Runs after the instance is reachable, retried every 15 seconds until it succeeds or times out. A
              failure never stops the instance — it's flagged for someone to look at.
            </p>
            <HookWhat
              label="Post-start"
              mode={postStartMode}
              onMode={setPostStartMode}
              command={postStartCommand}
              onCommand={setPostStartCommand}
              script={postStartScript}
              onScript={setPostStartScript}
              inherited={inheritedLabel('post_start')}
              example="e.g. pg_isready -q, or /opt/app/hooks/ready.sh"
            />
            <label className="block text-xs font-medium text-slate-500">
              Timeout (seconds, total)
              <input
                aria-label="Post-start timeout"
                type="number"
                min={1}
                max={MAX_TIMEOUT_S}
                className={`${inputClass} w-28`}
                value={postStartTimeout}
                onChange={(e) => setPostStartTimeout(e.target.value)}
              />
            </label>
          </fieldset>

          {actionError && <ErrorBanner message={actionError} />}
          {saveWarnings && <WarningBanner messages={saveWarnings} />}
          {runResult && (
            <div
              className={`rounded-md px-3 py-2 text-xs ${runResult.ok ? 'bg-emerald-50 text-emerald-800' : 'bg-red-50 text-red-800'}`}
            >
              <p className="font-medium">{runResult.summary}</p>
              {runResult.output_tail && (
                <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap font-mono">
                  {runResult.output_tail}
                </pre>
              )}
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <Button variant="primary" disabled={busy !== null} onClick={save}>
              {busy === 'save' ? <Spinner /> : 'Save hooks'}
            </Button>
            {target.kind === 'instance' && effectivePostStart && (
              <Button
                disabled={busy !== null || !instanceRunning}
                onClick={runCheck}
                title={instanceRunning ? undefined : 'The instance must be running.'}
              >
                {busy === 'run' ? <Spinner /> : 'Run post-start check now'}
              </Button>
            )}
          </div>

          {target.kind === 'instance' && events.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Hook activity</h4>
              <ul className="mt-2 divide-y divide-slate-100 text-xs">
                {events.map((event, i) => (
                  <li key={i} className="flex items-start justify-between gap-3 py-2">
                    <div className="min-w-0">
                      <span className="font-medium text-slate-900">{hookLabel(event.hook)}</span>
                      {event.detail && <p className="mt-0.5 break-words text-slate-500">{event.detail}</p>}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className={resultClass(event.result)}>{event.result}</span>
                      <span className="text-slate-400">{new Date(event.timestamp).toLocaleString()}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}
function HookWhat({
  label,
  mode,
  onMode,
  command,
  onCommand,
  script,
  onScript,
  inherited,
  example,
}: {
  label: string
  mode: HookMode
  onMode: (m: HookMode) => void
  command: string
  onCommand: (v: string) => void
  script: string
  onScript: (v: string) => void
  inherited: string | null
  example: string
}) {
  const [fileError, setFileError] = useState<string | null>(null)
  return (
    <div className="space-y-2">
      <div className="flex gap-4 text-xs text-slate-600" role="radiogroup" aria-label={`${label} hook type`}>
        <label className="flex items-center gap-1">
          <input type="radio" checked={mode === 'command'} onChange={() => onMode('command')} />
          Command or script path on the instance
        </label>
        <label className="flex items-center gap-1">
          <input type="radio" checked={mode === 'script'} onChange={() => onMode('script')} />
          Upload a script
        </label>
      </div>
      {mode === 'command' ? (
        <textarea
          aria-label={`${label} command`}
          rows={2}
          className={`${inputClass} font-mono`}
          placeholder={inherited !== null ? `Inherited from group: ${inherited}` : example}
          value={command}
          onChange={(e) => onCommand(e.target.value)}
        />
      ) : (
        <>
          <textarea
            aria-label={`${label} script`}
            rows={6}
            className={`${inputClass} font-mono`}
            placeholder={'#!/bin/bash\n# Stored by the scheduler, copied to the instance and run each time.'}
            value={script}
            onChange={(e) => onScript(e.target.value)}
          />
          <input
            aria-label={`${label} script file`}
            type="file"
            className="block text-xs text-slate-600"
            onChange={(e) => {
              const file = e.target.files?.[0]
              setFileError(null)
              if (!file) return
              if (file.size > MAX_SCRIPT_BYTES) {
                setFileError('That file is larger than 1 MB.')
                return
              }
              void file.text().then(onScript)
            }}
          />
          {fileError && <p className="text-xs text-red-600">{fileError}</p>}
        </>
      )}
    </div>
  )
}
function hookLabel(hook: HookEvent['hook']): string {
  return hook === 'pre_stop' ? 'Pre-stop hook' : hook === 'post_start' ? 'Post-start check' : 'Configuration'
}
function resultClass(result: HookEvent['result']): string {
  if (result === 'success') return 'font-medium text-emerald-600'
  if (result === 'failure') return 'font-medium text-red-600'
  if (result === 'warning' || result === 'skipped') return 'font-medium text-amber-600'
  return 'font-medium text-slate-500'
}
