import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { SystemBackupStatus } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, Spinner } from '../components/ui'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e)
}
const INPUT =
  'mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
export function SystemBackupPage() {
  const statusBar = useStatusBar()
  const [status, setStatus] = useState<SystemBackupStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ bucket: '', endpoint: '', access_key: '', secret_key: '' })
  const [busy, setBusy] = useState<string | null>(null)
  const runningRef = useRef(false)
  const reload = useCallback(() => {
    api.getSystemBackup().then(
      (s) => {
        setStatus(s)
        setError(null)
      },
      (e) => setError(message(e)),
    )
  }, [])
  useEffect(() => {
    reload()
  }, [reload])
  async function act(label: string, fn: () => Promise<string | null>) {
    if (runningRef.current) return
    runningRef.current = true
    setBusy(label)
    setError(null)
    setNotice(null)
    try {
      const done = await fn()
      if (done) setNotice(done)
      reload()
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(null)
      runningRef.current = false
    }
  }
  const obj = status?.object_storage
  const last = status?.last_backup
  const formComplete = Object.values(form).every((v) => v.trim() !== '')
  return (
    <>
      <PageHeader
        title="System backup"
        subtitle="Backups of this tool itself: its database (schedules, groups, history, API tokens), instance records and trusted host keys."
      />
      <div className="space-y-6 px-8 py-6">
        {error && <ErrorBanner message={error} />}
        {notice && <p className="rounded-md bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p>}
        {status === null ? (
          <div className="flex justify-center p-8">
            <Spinner />
          </div>
        ) : (
          <>
            <Card>
              <CardHeader
                title="Last backup"
                subtitle="The scheduled backup (hourly by default) and “Back up now” both record their result here."
                action={
                  <Button
                    variant="primary"
                    disabled={busy !== null}
                    onClick={() =>
                      act('run', async () => {
                        const r = await statusBar.run('Backing up the scheduler', (report) =>
                          api.runSystemBackup(report),
                        )
                        return r.ok
                          ? 'Backup completed.'
                          : `Backup finished with problems: ${r.problems.join('; ')}`
                      })
                    }
                  >
                    {busy === 'run' ? 'Backing up…' : 'Back up now'}
                  </Button>
                }
              />
              <div className="px-5 py-4 text-sm">
                {!last ? (
                  <p className="text-slate-600">No backup has run yet.</p>
                ) : (
                  <div className="space-y-1">
                    <p className={last.ok ? 'text-emerald-700' : 'text-rose-700'}>
                      {last.ok ? 'OK' : 'Problems'} — {new Date(last.finished_at).toLocaleString()}
                      {last.trigger
                        ? ` (${last.trigger === 'api' ? 'dashboard/API' : 'scheduled or command line'})`
                        : ''}
                    </p>
                    {last.object_storage_key && (
                      <p className="text-slate-600">Object Storage: {last.object_storage_key}</p>
                    )}
                    {last.local_path && <p className="text-slate-600">On this host: {last.local_path}</p>}
                    {last.problems.map((p) => (
                      <p key={p} className="text-rose-700">
                        {p}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader
                title="Off-host backups (Linode Object Storage)"
                subtitle="Strongly recommended: without it, backups stay on this host and are lost with it."
                action={
                  obj?.configured && !editing ? (
                    <div className="flex gap-2">
                      <Button
                        disabled={busy !== null}
                        onClick={() =>
                          act('test', async () => {
                            await api.testSystemBackup()
                            return 'Connection test passed (a test object was written, read back and deleted).'
                          })
                        }
                      >
                        {busy === 'test' ? 'Testing…' : 'Test connection'}
                      </Button>
                      <Button
                        disabled={busy !== null}
                        onClick={() => {
                          setForm({
                            bucket: obj.bucket ?? '',
                            endpoint: obj.endpoint ?? '',
                            access_key: '',
                            secret_key: '',
                          })
                          setEditing(true)
                        }}
                      >
                        Change
                      </Button>
                    </div>
                  ) : undefined
                }
              />
              <div className="px-5 py-4 text-sm space-y-3">
                {obj?.configured && !editing && (
                  <div className="space-y-1 text-slate-700">
                    <p>
                      Bucket <strong>{obj.bucket}</strong> at {obj.endpoint}
                    </p>
                    <p>Access key {obj.access_key_hint}; secret key stored (never shown).</p>
                    <Button
                      variant="ghost"
                      disabled={busy !== null}
                      onClick={() =>
                        act('disable', async () => {
                          await api.disableSystemBackup()
                          return 'Object Storage backups turned off.'
                        })
                      }
                    >
                      Turn off off-host backups
                    </Button>
                  </div>
                )}
                {(!obj?.configured || editing) && (
                  <form
                    className="grid max-w-xl gap-3"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void act('save', async () => {
                        await api.configureSystemBackup(form)
                        setEditing(false)
                        setForm({ bucket: '', endpoint: '', access_key: '', secret_key: '' })
                        return 'Saved. The connection test passed; backups now also go to Object Storage.'
                      })
                    }}
                  >
                    {!obj?.configured && (
                      <p className="text-amber-700">
                        Not configured — backups currently stay on this host only.
                      </p>
                    )}
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">Bucket</span>
                      <input
                        className={INPUT}
                        value={form.bucket}
                        onChange={(e) => setForm({ ...form, bucket: e.target.value })}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">
                        Endpoint (e.g. https://in-maa-1.linodeobjects.com)
                      </span>
                      <input
                        className={INPUT}
                        value={form.endpoint}
                        onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">Access key</span>
                      <input
                        className={INPUT}
                        autoComplete="off"
                        value={form.access_key}
                        onChange={(e) => setForm({ ...form, access_key: e.target.value })}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">Secret key</span>
                      <input
                        className={INPUT}
                        type="password"
                        autoComplete="new-password"
                        value={form.secret_key}
                        onChange={(e) => setForm({ ...form, secret_key: e.target.value })}
                      />
                    </label>
                    <p className="text-xs text-slate-500">
                      The settings are tested (a small test object is written, read back and deleted) before
                      they are saved. Use a key limited to this bucket.
                    </p>
                    <div className="flex gap-2">
                      <Button variant="primary" type="submit" disabled={!formComplete || busy !== null}>
                        {busy === 'save' ? 'Testing and saving…' : 'Test and save'}
                      </Button>
                      {editing && (
                        <Button variant="ghost" type="button" onClick={() => setEditing(false)}>
                          Cancel
                        </Button>
                      )}
                    </div>
                  </form>
                )}
              </div>
            </Card>

            <Card>
              <CardHeader title="On this host" />
              <p className="px-5 py-4 text-sm text-slate-700">
                {status.local_backup_dir ? (
                  <>
                    Snapshots are also written to <code>{status.local_backup_dir}</code>.
                  </>
                ) : (
                  'No local backup folder is set up.'
                )}
              </p>
            </Card>
          </>
        )}
      </div>
    </>
  )
}
