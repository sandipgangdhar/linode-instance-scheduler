import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Holiday, ScheduleGroupSummary } from '../api/types'
import { Button, Card, CardHeader, EmptyState, ErrorBanner, Spinner } from '../components/ui'
import { PageHeader } from './DashboardLayout'
const INPUT =
  'mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e)
}
function whoLabel(h: Holiday): string {
  return h.scope === 'all' ? 'Every node' : h.scope === 'group' ? `Group ${h.target}` : `Node ${h.target}`
}
export function HolidaysPage() {
  const [holidays, setHolidays] = useState<Holiday[] | null>(null)
  const [groups, setGroups] = useState<ScheduleGroupSummary[]>([])
  const [nodes, setNodes] = useState<string[]>([])
  const [showPast, setShowPast] = useState(false)
  const [form, setForm] = useState({ date: '', to: '', scope: 'all', target: '', note: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const busyRef = useRef(false)
  const reload = useCallback(() => {
    api.listHolidays(showPast).then(
      (list) => {
        setHolidays(list)
        setError(null)
      },
      (e) => setError(message(e)),
    )
  }, [showPast])
  useEffect(() => {
    reload()
  }, [reload])
  useEffect(() => {
    api.listGroups().then(setGroups, () => {})
    api.listInstances().then(
      (r) => setNodes(Object.keys(r).sort()),
      () => {},
    )
  }, [])
  async function act(fn: () => Promise<string>) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      setNotice(await fn())
      reload()
    } catch (e) {
      setError(message(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  const scopeArgs = {
    group_name: form.scope === 'group' ? form.target : null,
    name: form.scope === 'instance' ? form.target : null,
  }
  const canAdd = form.date !== '' && (form.scope === 'all' || form.target !== '')
  return (
    <>
      <PageHeader
        title="Holidays"
        subtitle="On these dates scheduled starts are skipped, so the infrastructure stays down. Scheduled stops still happen, and manual starts are never blocked."
      />
      <div className="space-y-6 px-8 py-6">
        {error && <ErrorBanner message={error} />}
        {notice && <p className="rounded-md bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p>}
        <Card>
          <CardHeader
            title="Add a holiday"
            subtitle="A single date or a range. Dates are in each schedule's own timezone."
          />
          <form
            className="grid max-w-3xl grid-cols-2 gap-3 px-5 py-4 text-sm"
            onSubmit={(e) => {
              e.preventDefault()
              void act(async () => {
                const r = await api.addHolidays({
                  date: form.date,
                  to: form.to || null,
                  note: form.note || null,
                  ...scopeArgs,
                })
                setForm({ ...form, date: '', to: '', note: '' })
                return r.added.length ? `Added: ${r.added.join(', ')}.` : 'Already on the list.'
              })
            }}
          >
            <label className="block">
              <span className="text-xs font-medium text-slate-500">Date</span>
              <input
                aria-label="Holiday date"
                type="date"
                className={INPUT}
                value={form.date}
                onChange={(e) => setForm({ ...form, date: e.target.value })}
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-slate-500">Until (optional, inclusive)</span>
              <input
                aria-label="Holiday end date"
                type="date"
                className={INPUT}
                value={form.to}
                onChange={(e) => setForm({ ...form, to: e.target.value })}
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-slate-500">Applies to</span>
              <select
                aria-label="Holiday applies to"
                className={INPUT}
                value={form.scope}
                onChange={(e) => setForm({ ...form, scope: e.target.value, target: '' })}
              >
                <option value="all">Every node</option>
                <option value="group">One group</option>
                <option value="instance">One node</option>
              </select>
            </label>
            {form.scope !== 'all' ? (
              <label className="block">
                <span className="text-xs font-medium text-slate-500">
                  {form.scope === 'group' ? 'Group' : 'Node'}
                </span>
                <select
                  aria-label="Holiday target"
                  className={INPUT}
                  value={form.target}
                  onChange={(e) => setForm({ ...form, target: e.target.value })}
                >
                  <option value="">Choose…</option>
                  {(form.scope === 'group' ? groups.map((g) => g.name) : nodes).map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <div />
            )}
            <label className="col-span-2 block">
              <span className="text-xs font-medium text-slate-500">Note (optional)</span>
              <input
                aria-label="Holiday note"
                className={INPUT}
                maxLength={200}
                value={form.note}
                onChange={(e) => setForm({ ...form, note: e.target.value })}
              />
            </label>
            <div className="col-span-2">
              <Button variant="primary" type="submit" disabled={!canAdd || busy}>
                {busy ? 'Saving…' : 'Add holiday'}
              </Button>
            </div>
          </form>
        </Card>
        <Card>
          <CardHeader
            title="Holidays"
            action={
              <label className="flex items-center gap-2 text-sm text-slate-600">
                <input type="checkbox" checked={showPast} onChange={(e) => setShowPast(e.target.checked)} />{' '}
                Show past
              </label>
            }
          />
          {holidays === null ? (
            <div className="flex justify-center p-8">
              <Spinner />
            </div>
          ) : holidays.length === 0 ? (
            <EmptyState
              title="No holidays"
              subtitle="Add one above, or use “Skip tomorrow” on a group's page."
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {holidays.map((h) => (
                <li
                  key={`${h.date}-${h.scope}-${h.target ?? ''}`}
                  className="flex items-center justify-between gap-3 px-5 py-3 text-sm"
                >
                  <div>
                    <span className="font-medium text-slate-900">{h.date}</span>
                    <span className="ml-3 text-slate-600">{whoLabel(h)}</span>
                    {h.note && <span className="ml-3 text-slate-500">— {h.note}</span>}
                  </div>
                  <Button
                    variant="ghost"
                    disabled={busy}
                    onClick={() =>
                      act(async () => {
                        await api.removeHolidays({
                          date: h.date,
                          group_name: h.scope === 'group' ? h.target : null,
                          name: h.scope === 'instance' ? h.target : null,
                        })
                        return `Removed ${h.date} (${whoLabel(h)}).`
                      })
                    }
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
