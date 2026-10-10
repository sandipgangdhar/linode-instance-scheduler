import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Holiday, ScheduleGroup } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
export function tomorrowIn(timezone: string, now: Date = new Date()): string {
  const next = new Date(now.getTime() + 24 * 3600 * 1000)
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(next)
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}`
}
export function groupSkipsOn(
  date: string,
  holidays: Holiday[],
  groupName: string,
  ignoresAccountWide: boolean,
): boolean {
  return holidays.some(
    (h) =>
      h.date === date &&
      ((h.scope === 'group' && h.target === groupName) || (h.scope === 'all' && !ignoresAccountWide)),
  )
}
export function GroupHolidayCard({ group, onChanged }: { group: ScheduleGroup; onChanged?: () => void }) {
  const [holidays, setHolidays] = useState<Holiday[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [date, setDate] = useState('')
  const [to, setTo] = useState('')
  const [note, setNote] = useState('')
  const [chosen, setChosen] = useState<boolean | null>(null)
  const ignores = chosen ?? group.account_holidays === 'ignore'
  const busyRef = useRef(false)
  const reload = useCallback(() => {
    api.listHolidays().then(
      (all) =>
        setHolidays(all.filter((h) => h.scope === 'all' || (h.scope === 'group' && h.target === group.name))),
      () => {},
    )
  }, [group.name])
  useEffect(() => {
    reload()
  }, [reload])
  const tomorrow = tomorrowIn(group.timezone)
  const skipped = groupSkipsOn(tomorrow, holidays, group.name, ignores)
  async function act(fn: () => Promise<string | null>) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    setNotice(null)
    setWarnings([])
    try {
      setNotice(await fn())
      reload()
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  const skipTomorrow = () =>
    act(async () => {
      await api.addHolidays({ date: tomorrow, group_name: group.name, note: 'Skipped from the group page' })
      return `Tomorrow (${tomorrow}) is now a holiday for this group: its scheduled starts are skipped.`
    })
  const addHoliday = () =>
    act(async () => {
      const r = await api.addHolidays({
        date,
        to: to || null,
        group_name: group.name,
        note: note.trim() || null,
      })
      setWarnings(r.warnings ?? [])
      setDate('')
      setTo('')
      setNote('')
      return r.added.length
        ? `Added for this group: ${r.added.join(', ')}.`
        : 'Those dates were already holidays for this group.'
    })
  const removeHoliday = (h: Holiday) =>
    act(async () => {
      const r = await api.removeHolidays({ date: h.date, group_name: group.name })
      setWarnings(r.warnings ?? [])
      return `Removed ${h.date}.`
    })
  const toggleAccountWide = (ignore: boolean) =>
    act(async () => {
      setChosen(ignore)
      let r
      try {
        r = await api.setGroupHolidaySetting(group.name, ignore ? 'ignore' : 'follow')
      } catch (e) {
        setChosen(null)
        throw e
      }
      setWarnings(r.warnings ?? [])
      onChanged?.()
      return ignore
        ? 'This group now runs on account-wide holidays (its own holidays still apply).'
        : 'This group now follows the account-wide holiday calendar.'
    })
  return (
    <Card>
      <CardHeader
        title="Holidays"
        subtitle="On a holiday this group's members aren't started by the schedule; scheduled stops still happen."
        action={
          <Button variant="secondary" disabled={busy || skipped} onClick={skipTomorrow}>
            {skipped ? 'Tomorrow is a holiday' : busy ? 'Saving…' : 'Skip tomorrow'}
          </Button>
        }
      />
      <div className="space-y-4 px-5 py-4 text-sm">
        <label className="flex items-start gap-2 text-slate-700">
          <input
            type="checkbox"
            aria-label="Run on account-wide holidays"
            className="mt-0.5"
            checked={ignores}
            disabled={busy}
            onChange={(e) => toggleAccountWide(e.target.checked)}
          />
          <span>
            <strong>Run on account-wide holidays</strong>
            <span className="block text-xs text-slate-500">
              Account-wide holidays won't skip this group's starts. Holidays added for this group still do,
              and a member's own holiday setting overrides this.
            </span>
          </span>
        </label>

        {holidays.length === 0 ? (
          <p className="text-slate-500">No upcoming holidays for this group.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {holidays.map((h) => {
              const own = h.scope === 'group'
              return (
                <li key={`${h.date}-${h.scope}`} className="flex items-center justify-between py-1.5">
                  <span className={own || !ignores ? 'text-slate-700' : 'text-slate-400 line-through'}>
                    {h.date} — {own ? 'this group' : 'account-wide'}
                    {h.note ? ` (${h.note})` : ''}
                    {!own && ignores ? ' · not applied' : ''}
                  </span>
                  {own && (
                    <Button variant="ghost" disabled={busy} onClick={() => removeHoliday(h)}>
                      Remove
                    </Button>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-slate-600">
            Date
            <input
              type="date"
              aria-label="Group holiday date"
              className="mt-1 block rounded-md border border-slate-300 px-2 py-1 text-sm"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </label>
          <label className="text-xs text-slate-600">
            To (optional)
            <input
              type="date"
              aria-label="Group holiday end date"
              className="mt-1 block rounded-md border border-slate-300 px-2 py-1 text-sm"
              value={to}
              min={date || undefined}
              onChange={(e) => setTo(e.target.value)}
            />
          </label>
          <label className="flex-1 text-xs text-slate-600">
            Note
            <input
              aria-label="Group holiday note"
              className="mt-1 block w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. Diwali"
            />
          </label>
          <Button variant="primary" disabled={busy || !date} onClick={addHoliday}>
            Add for this group
          </Button>
        </div>
        {notice && <p className="text-emerald-700">{notice}</p>}
        {warnings.length > 0 && <WarningBanner messages={warnings} />}
        {error && <ErrorBanner message={error} />}
      </div>
    </Card>
  )
}
