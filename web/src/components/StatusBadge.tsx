import type { InstanceStatus } from '../api/types'
const STYLES: Record<InstanceStatus, string> = {
  running: 'bg-emerald-100 text-emerald-800 ring-emerald-600/20',
  stopped: 'bg-slate-100 text-slate-700 ring-slate-500/20',
  unreachable: 'bg-amber-100 text-amber-800 ring-amber-600/20',
  needs_manual_recovery: 'bg-red-100 text-red-800 ring-red-600/20',
}
const DOT: Record<InstanceStatus, string> = {
  running: 'bg-emerald-500',
  stopped: 'bg-slate-400',
  unreachable: 'bg-amber-500',
  needs_manual_recovery: 'bg-red-500',
}
export function StatusBadge({ status, locked }: { status: InstanceStatus; locked?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${STYLES[status]}`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${DOT[status]}`} />
        {status.replace(/_/g, ' ')}
      </span>
      {locked && (
        <span className="inline-flex items-center rounded-full bg-indigo-100 px-2.5 py-1 text-xs font-medium text-indigo-800 ring-1 ring-inset ring-indigo-600/20">
          transitioning
        </span>
      )}
    </span>
  )
}
