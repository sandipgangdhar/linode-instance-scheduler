import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Holiday, ScheduleGroup } from '../api/types'
import { Button, Card, CardHeader, ErrorBanner } from './ui'
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
export function GroupHolidayCard({ group }: { group: ScheduleGroup }) {
  const [holidays, setHolidays] = useState<Holiday[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
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
  const skipped = holidays.some((h) => h.date === tomorrow)
  async function skipTomorrow() {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    try {
      await api.addHolidays({ date: tomorrow, group_name: group.name, note: 'Skipped from the group page' })
      setNotice(`Tomorrow (${tomorrow}) is now a holiday for this group: its scheduled starts are skipped.`)
      reload()
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
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
      <div className="space-y-2 px-5 py-4 text-sm">
        {holidays.length === 0 ? (
          <p className="text-slate-500">
            No upcoming holidays for this group. Manage them all on the Holidays page.
          </p>
        ) : (
          <ul className="space-y-1">
            {holidays.map((h) => (
              <li key={`${h.date}-${h.scope}`} className="text-slate-700">
                {h.date} — {h.scope === 'all' ? 'every node' : 'this group'}
                {h.note ? ` (${h.note})` : ''}
              </li>
            ))}
          </ul>
        )}
        {notice && <p className="text-emerald-700">{notice}</p>}
        {error && <ErrorBanner message={error} />}
      </div>
    </Card>
  )
}
