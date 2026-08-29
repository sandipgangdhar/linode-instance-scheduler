import { useState } from 'react'
import { useStatusBar } from '../status/StatusBarContext'
import { ProgressBar, Spinner } from './ui'
const KIND_CLASSES = {
  pending: 'bg-slate-900 text-white',
  success: 'bg-emerald-600 text-white',
  error: 'bg-red-600 text-white',
} as const
export function StatusBar() {
  const { current, others, bringToFront, dismiss } = useStatusBar()
  const [showOthers, setShowOthers] = useState(false)
  if (!current) return null
  const clickable = current.onNavigate !== undefined
  return (
    <div className="fixed inset-x-0 bottom-0 z-50 shadow-lg sm:left-60">
      <div
        className={`flex items-center justify-between gap-3 px-5 py-2.5 text-sm ${KIND_CLASSES[current.kind]} ${clickable ? 'cursor-pointer' : ''}`}
        role="status"
        onClick={current.onNavigate}
      >
        <div className="flex min-w-0 items-center gap-2">
          {current.kind === 'pending' && <Spinner className="shrink-0 text-white" />}
          {current.kind === 'success' && <CheckIcon className="h-4 w-4 shrink-0" />}
          {current.kind === 'error' && <ErrorIcon className="h-4 w-4 shrink-0" />}
          <span className="truncate font-medium">{current.label}</span>
          {current.kind === 'pending' && typeof current.percent === 'number' && (
            <span className="shrink-0 text-white/80">{current.percent}%</span>
          )}
          {current.detail && <span className="truncate text-white/80">— {current.detail}</span>}
          {clickable && (
            <span className="shrink-0 text-xs text-white/60 underline underline-offset-2">click to view</span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {others.length > 0 && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                setShowOthers((s) => !s)
              }}
              className="shrink-0 rounded px-2 py-0.5 text-xs text-white/80 underline underline-offset-2 hover:text-white"
            >
              +{others.length} more
            </button>
          )}
          {current.kind !== 'pending' && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                dismiss(current.id)
              }}
              className="shrink-0 rounded px-2 py-0.5 text-white/70 transition hover:bg-white/10 hover:text-white"
              aria-label="Dismiss"
            >
              ✕
            </button>
          )}
        </div>
      </div>
      {current.kind === 'pending' && typeof current.percent === 'number' && (
        <ProgressBar
          percent={current.percent}
          trackClassName="h-0.5 bg-white/20"
          fillClassName="bg-white"
          minPercent={2}
        />
      )}
      {showOthers && others.length > 0 && (
        <div className="max-h-48 overflow-y-auto border-t border-white/10 bg-slate-950">
          {others.map((o) => (
            <div
              key={o.id}
              role={o.onNavigate ? 'button' : undefined}
              onClick={() => {
                bringToFront(o.id)
                setShowOthers(false)
                o.onNavigate?.()
              }}
              className={`flex items-center justify-between gap-3 px-5 py-2 text-xs text-white/90 hover:bg-white/5 ${o.onNavigate ? 'cursor-pointer' : ''}`}
            >
              <div className="flex min-w-0 items-center gap-2">
                {o.kind === 'pending' && <Spinner className="h-3 w-3 shrink-0 text-white" />}
                {o.kind === 'success' && <CheckIcon className="h-3 w-3 shrink-0 text-emerald-400" />}
                {o.kind === 'error' && <ErrorIcon className="h-3 w-3 shrink-0 text-red-400" />}
                <span className="truncate">{o.label}</span>
                {o.kind === 'pending' && typeof o.percent === 'number' && (
                  <span className="shrink-0 text-white/60">{o.percent}%</span>
                )}
              </div>
              {o.onNavigate && (
                <span className="shrink-0 text-white/50 underline underline-offset-2">switch to this</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
    </svg>
  )
}
function ErrorIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
    </svg>
  )
}
