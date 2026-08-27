import { useState } from 'react'
import type { Schedule, ScheduleRule } from '../api/types'
import { VALID_DAYS } from '../api/types'
import { TimezoneSelect } from './TimezoneSelect'
import { Button } from './ui'
const EMPTY_RULE: ScheduleRule = { days_of_week: [], start_time: '09:00', stop_time: '18:00' }
export function ScheduleEditor({
  initial,
  onSave,
  saving,
}: {
  initial: Schedule | null
  onSave: (schedule: Schedule) => void
  saving: boolean
}) {
  const [timezone, setTimezone] = useState(initial?.timezone ?? 'UTC')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [rules, setRules] = useState<ScheduleRule[]>(
    initial?.rules.length ? initial.rules : [{ ...EMPTY_RULE }],
  )
  const updateRule = (index: number, patch: Partial<ScheduleRule>) => {
    setRules((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)))
  }
  const toggleDay = (index: number, day: string) => {
    setRules((prev) =>
      prev.map((r, i) => {
        if (i !== index) return r
        const has = r.days_of_week.includes(day)
        return {
          ...r,
          days_of_week: has ? r.days_of_week.filter((d) => d !== day) : [...r.days_of_week, day],
        }
      }),
    )
  }
  const removeRule = (index: number) => setRules((prev) => prev.filter((_, i) => i !== index))
  const addRule = () => setRules((prev) => [...prev, { ...EMPTY_RULE }])
  const hasValidTimeOrder = rules.every(
    (r) => r.start_time !== '' && r.stop_time !== '' && r.start_time < r.stop_time,
  )
  const canSave = rules.length > 0 && rules.every((r) => r.days_of_week.length > 0) && hasValidTimeOrder
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-4">
        <label className="block">
          <span className="text-sm font-medium text-slate-700">Timezone</span>
          <TimezoneSelect value={timezone} onChange={setTimezone} />
        </label>
        <label className="flex items-center gap-2 pt-6">
          <input
            type="checkbox"
            className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-600"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span className="text-sm font-medium text-slate-700">Enabled</span>
        </label>
      </div>

      <div className="space-y-3">
        {rules.map((rule, i) => (
          <div key={i} className="rounded-md border border-slate-200 p-3">
            <div className="flex flex-wrap gap-1.5">
              {VALID_DAYS.map((day) => (
                <button
                  key={day}
                  type="button"
                  onClick={() => toggleDay(i, day)}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium capitalize transition ${
                    rule.days_of_week.includes(day)
                      ? 'bg-indigo-600 text-white'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  }`}
                >
                  {day}
                </button>
              ))}
            </div>
            <div className="mt-3 flex items-end gap-3">
              <label className="block">
                <span className="text-xs font-medium text-slate-500">Start</span>
                <input
                  type="time"
                  className="mt-1 block rounded-md border-0 py-1.5 px-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                  value={rule.start_time}
                  onChange={(e) => updateRule(i, { start_time: e.target.value })}
                />
              </label>
              <label className="block">
                <span className="text-xs font-medium text-slate-500">Stop</span>
                <input
                  type="time"
                  className="mt-1 block rounded-md border-0 py-1.5 px-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                  value={rule.stop_time}
                  onChange={(e) => updateRule(i, { stop_time: e.target.value })}
                />
              </label>
              {rules.length > 1 && (
                <Button variant="ghost" type="button" onClick={() => removeRule(i)} className="mb-0.5">
                  Remove
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between">
        <Button variant="ghost" type="button" onClick={addRule}>
          + Add another rule
        </Button>
        <Button
          variant="primary"
          type="button"
          disabled={!canSave || saving}
          onClick={() => onSave({ timezone, rules, enabled })}
        >
          {saving ? 'Saving…' : 'Save schedule'}
        </Button>
      </div>
      {!canSave && (
        <p className="text-xs text-amber-600">
          {!hasValidTimeOrder
            ? 'Start time must be before stop time -- overnight schedules are not supported yet.'
            : 'Every rule needs at least one day selected.'}
        </p>
      )}
    </div>
  )
}
