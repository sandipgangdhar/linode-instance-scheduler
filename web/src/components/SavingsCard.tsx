import type { Savings } from '../api/types'
function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-3xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  )
}
function fmt(value: number | null): string {
  return value === null ? '—' : `${value}%`
}
export function SavingsCard({ savings }: { savings: Savings | null }) {
  if (!savings) return null
  return (
    <div className="grid grid-cols-2 gap-6">
      <Stat
        label="Scheduled savings"
        value={fmt(savings.scheduled_savings_percent)}
        hint={
          savings.scheduled_savings_percent !== null
            ? 'vs. running 24/7'
            : savings.schedule_state === 'disabled'
              ? 'Schedule disabled'
              : 'No schedule set yet'
        }
      />
      <Stat
        label="Actual savings"
        value={fmt(savings.actual_savings_percent)}
        hint={
          savings.note
            ? 'See each member individually'
            : savings.actual_savings_percent === null
              ? 'Not enough history yet'
              : `Last ${savings.window_days} days`
        }
      />
    </div>
  )
}
